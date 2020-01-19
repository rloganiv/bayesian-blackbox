import argparse
import logging
import pathlib
import random
from collections import deque
from typing import List, Dict, Tuple

import matplotlib.pyplot as plt
import numpy as np
from active_utils import prepare_data, SAMPLE_CATEGORY, _get_confidence_k, _get_ground_truth
from data_utils import datafile_dict, num_classes_dict, DATASET_LIST
from models import BetaBernoulli, ClasswiseEce
from tqdm import tqdm

COLUMN_WIDTH = 3.25  # Inches
TEXT_WIDTH = 6.299213  # Inches
GOLDEN_RATIO = 1.61803398875
DPI = 300
FONT_SIZE = 8
OUTPUT_DIR = "../output_from_anvil/active_learning_topk"
RUNS = 100
LOG_FREQ = 10


def get_samples_topk(args: argparse.Namespace,
                     categories: List[int],
                     observations: List[bool],
                     confidences: List[float],
                     num_classes: int,
                     num_samples: int,
                     sample_method: str,
                     prior=None,
                     weight=None,
                     random_seed: int = 0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    # prepare model, deques, thetas, choices

    random.seed(random_seed)

    if args.metric == 'accuracy':
        model = BetaBernoulli(num_classes, prior)
    elif args.metric == 'calibration_error':
        model = ClasswiseEce(num_classes, num_bins=10, weight=weight, prior=None)

    deques = [deque() for _ in range(num_classes)]
    for (category, score, observation) in zip(categories, confidences, observations):
        if args.metric == 'accuracy':
            deques[category].append(observation)
        elif args.metric == 'calibration_error':
            deques[category].append((observation, score))
    for _deque in deques:
        random.shuffle(_deque)

    sampled_categories = np.empty((num_samples,), dtype=int)
    sampled_scores = np.empty((num_samples,), dtype=float)
    sampled_observations = np.empty((num_samples,), dtype=bool)

    idx = 0

    topk = args.topk

    while idx < num_samples:
        # sampling process:
        # if there are less than k available arms to play, switch to top 1, the sampling method has been switched to top1,
        # then the return 'category_list' is an int
        categories_list = SAMPLE_CATEGORY[sample_method].__call__(deques=deques,
                                                                  random_seed=random_seed,
                                                                  model=model,
                                                                  mode=args.mode,
                                                                  topk=topk,
                                                                  max_ttts_trial=50,
                                                                  ttts_beta=0.5,
                                                                  epsilon=0.1,
                                                                  ucb_c=1, )
        if type(categories_list) != list:
            categories_list = [categories_list]
            if topk != 1:
                topk = 1

        # update model, deques, thetas, choices
        for category in categories_list:
            if args.metric == 'accuracy':
                observation = deques[category].pop()
                model.update(category, observation)

            elif args.metric == 'calibration_error':
                observation, score = deques[category].pop()
                model.update(category, observation, score)
                sampled_scores[idx] = score

            sampled_categories[idx] = category
            sampled_observations[idx] = observation

            idx += 1

    return sampled_categories, sampled_observations, sampled_scores


def eval(args: argparse.Namespace,
         categories: List[int],
         observations: List[bool],
         confidences: List[float],
         ground_truth: np.ndarray,
         num_classes: int,
         prior=None,
         weight=None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """

    :param args:
    :param categories:
    :param observations:
    :param confidences:
    :param ground_truth:
    :param num_classes:
    :param prior:
    :param weight:

    :return avg_num_agreement: (num_samples, ) array.
            Average number of agreement between selected topk and ground truth topk at each step.
    :return cumulative_metric: (num_samples, ) array.
            Metric (accuracy or ece) measured on sampled_observations, sampled categories and sampled scores.
    :return non_cumulative_metric: (num_samples, ) array.
            Average metric (accuracy or ece) evaluated with model.eval of the selected topk arms at each step.
    """
    num_samples = len(categories)

    if args.metric == 'accuracy':
        model = BetaBernoulli(num_classes, prior)
    elif args.metric == 'calibration_error':
        model = ClasswiseEce(num_classes, num_bins=10, weight=weight, prior=None)

    avg_num_agreement = np.zeros((num_samples // LOG_FREQ,))
    cumulative_metric = np.zeros((num_samples // LOG_FREQ,))
    non_cumulative_metric = np.zeros((num_samples // LOG_FREQ,))

    topk_arms = np.zeros((num_classes,), dtype=np.bool_)

    for idx, (category, observation, confidence) in enumerate(zip(categories, observations, confidences)):

        if args.metric == 'accuracy':
            model.update(category, observation)

        elif args.metric == 'calibration_error':
            model.update(category, observation, confidence)

        if idx % LOG_FREQ == 0:
            # select TOPK arms
            topk_arms[:] = 0
            metric_val = model.eval
            if args.mode == 'min':
                indices = metric_val.argsort()[:args.topk]
            elif args.mode == 'max':
                indices = metric_val.argsort()[-args.topk:]
            topk_arms[indices] = 1

            # evaluation
            avg_num_agreement[idx // LOG_FREQ] = np.logical_and(topk_arms, ground_truth).mean()
            # todo: each class is equally weighted by taking the mean. replace with frequency.(?)
            cumulative_metric[idx // LOG_FREQ] = model.frequentist_eval.mean()
            non_cumulative_metric[idx // LOG_FREQ] = metric_val[topk_arms].mean()

    return avg_num_agreement, cumulative_metric, non_cumulative_metric


def _comparison_plot(eval_result_dict: Dict[str, np.ndarray], figname: str, ylabel: str) -> None:
    # If labels are getting cut off make the figsize smaller
    plt.figure(figsize=(COLUMN_WIDTH, COLUMN_WIDTH / GOLDEN_RATIO), dpi=300)

    for method_name, metric_eval in eval_result_dict.items():
        x = np.arange(len(metric_eval)) * LOG_FREQ
        plt.plot(x[1:], metric_eval[1:], label=method_name)
    plt.xlabel('#Queries')
    plt.ylabel(ylabel)
    plt.legend()
    plt.yticks(fontsize=FONT_SIZE)
    plt.xticks(fontsize=FONT_SIZE)
    plt.ylim(0.0, 1.0)
    plt.savefig(figname, format='pdf', dpi=300, bbox_inches='tight')


def comparison_plot(args: argparse.Namespace,
                    experiment_name: str,
                    avg_num_agreement_dict: Dict[str, np.ndarray],
                    cumulative_metric_dict: Dict[str, np.ndarray],
                    non_cumulative_metric_dict: Dict[str, np.ndarray]) -> None:
    """

    :param args:
    :param experiment_name:
    :param avg_num_agreement_dict:
        Dict maps str to np.ndarray of shape (RUNS, num_samples // LOG_FREQ)
    :param cumulative_metric_dict:
        Dict maps str to np.ndarray of shape (RUNS, num_samples // LOG_FREQ)
    :param non_cumulative_metric_dict:
        Dict maps str to np.ndarray of shape (RUNS, num_samples // LOG_FREQ)
    :return:
    """

    # avg over runs
    if args.metric == 'accuracy':
        method_list = ['random', 'ts_uniform', 'ts_informed']
    elif args.metric == 'calibration_error':
        method_list = ['random', 'ts']

    for method in method_list:
        avg_num_agreement_dict[method] = avg_num_agreement_dict[method].mean(axis=0)
        cumulative_metric_dict[method] = cumulative_metric_dict[method].mean(axis=0)
        non_cumulative_metric_dict[method] = non_cumulative_metric_dict[method].mean(axis=0)

    _comparison_plot(avg_num_agreement_dict,
                     args.output / experiment_name / "avg_num_agreement.pdf",
                     'Avg number of agreement')
    _comparison_plot(cumulative_metric_dict,
                     args.output / experiment_name / "cumulative.pdf",
                     ('Cumulative %s' % args.metric))
    _comparison_plot(non_cumulative_metric_dict,
                     args.output / experiment_name / "non_cumulative.pdf",
                     ('Non cumulative %s' % args.metric))


def main_accuracy_topk(args: argparse.Namespace, SAMPLE=True, EVAL=True, PLOT=True) -> None:
    num_classes = num_classes_dict[args.dataset]

    categories, observations, confidences, idx2category, category2idx = prepare_data(datafile_dict[args.dataset], False)
    num_samples = len(observations)
    confidence = _get_confidence_k(categories, confidences, num_classes)

    UNIFORM_PRIOR = np.ones((num_classes, 2)) / 2
    INFORMED_PRIOR = np.array([confidence, 1 - confidence]).T

    experiment_name = '%s_%s_%s_top%d_runs%d' % (args.dataset, args.metric, args.mode, args.topk, RUNS)

    if not (args.output / experiment_name).is_dir():
        (args.output / experiment_name).mkdir()

    sampled_categories_dict = {
        'random': np.empty((RUNS, num_samples), dtype=int),
        'ts_uniform': np.empty((RUNS, num_samples), dtype=int),
        'ts_informed': np.empty((RUNS, num_samples), dtype=int),
    }
    sampled_observations_dict = {
        'random': np.empty((RUNS, num_samples), dtype=bool),
        'ts_uniform': np.empty((RUNS, num_samples), dtype=bool),
        'ts_informed': np.empty((RUNS, num_samples), dtype=bool),
    }
    sampled_scores_dict = {
        'random': np.empty((RUNS, num_samples), dtype=float),
        'ts_uniform': np.empty((RUNS, num_samples), dtype=float),
        'ts_informed': np.empty((RUNS, num_samples), dtype=float),
    }

    avg_num_agreement_dict = {
        'random': np.zeros((RUNS, num_samples // LOG_FREQ)),
        'ts_uniform': np.zeros((RUNS, num_samples // LOG_FREQ)),
        'ts_informed': np.zeros((RUNS, num_samples // LOG_FREQ)),
    }
    cumulative_metric_dict = {
        'random': np.zeros((RUNS, num_samples // LOG_FREQ)),
        'ts_uniform': np.zeros((RUNS, num_samples // LOG_FREQ)),
        'ts_informed': np.zeros((RUNS, num_samples // LOG_FREQ)),
    }
    non_cumulative_metric_dict = {
        'random': np.zeros((RUNS, num_samples // LOG_FREQ)),
        'ts_uniform': np.zeros((RUNS, num_samples // LOG_FREQ)),
        'ts_informed': np.zeros((RUNS, num_samples // LOG_FREQ)),
    }

    if SAMPLE:
        for r in tqdm(range(RUNS)):
            sampled_categories_dict['random'][r], sampled_observations_dict['random'][r], sampled_scores_dict['random'][
                r] = get_samples_topk(args,
                                      categories,
                                      observations,
                                      confidences,
                                      num_classes,
                                      num_samples,
                                      sample_method='random',
                                      prior=UNIFORM_PRIOR,
                                      random_seed=r)

            sampled_categories_dict['ts_uniform'][r], sampled_observations_dict['ts_uniform'][r], \
            sampled_scores_dict['ts_uniform'][r] = get_samples_topk(args,
                                                                    categories,
                                                                    observations,
                                                                    confidences,
                                                                    num_classes,
                                                                    num_samples,
                                                                    sample_method='ts',
                                                                    prior=UNIFORM_PRIOR,
                                                                    random_seed=r)
            sampled_categories_dict['ts_informed'][r], sampled_observations_dict['ts_informed'][r], \
            sampled_scores_dict['ts_informed'][r] = get_samples_topk(args,
                                                                     categories,
                                                                     observations,
                                                                     confidences,
                                                                     num_classes,
                                                                     num_samples,
                                                                     sample_method='ts',
                                                                     prior=INFORMED_PRIOR,
                                                                     random_seed=r)
        # write samples to file
        for method in ['random', 'ts_uniform', 'ts_informed']:
            np.save(args.output / experiment_name / ('sampled_categories_%s.npy' % method),
                    sampled_categories_dict[method])
            np.save(args.output / experiment_name / ('sampled_observations_%s.npy' % method),
                    sampled_observations_dict[method])
            np.save(args.output / experiment_name / ('sampled_scores_%s.npy' % method), sampled_scores_dict[method])
    else:
        # load sampled categories, scores and observations from file
        for method in ['random', 'ts_uniform', 'ts_informed']:
            sampled_categories_dict[method] = np.load(
                args.output / experiment_name / ('sampled_categories_%s.npy' % method))
            sampled_observations_dict[method] = np.load(
                args.output / experiment_name / ('sampled_observations_%s.npy' % method))
            sampled_scores_dict[method] = np.load(args.output / experiment_name / ('sampled_scores_%s.npy' % method))

    if EVAL:
        ground_truth = _get_ground_truth(categories, observations, confidences, num_classes, args.metric, args.mode,
                                         topk=args.topk)
        for r in tqdm(range(RUNS)):
            avg_num_agreement_dict['random'][r], cumulative_metric_dict['random'][r], \
            non_cumulative_metric_dict['random'][r] = eval(args,
                                                           sampled_categories_dict['random'][r].tolist(),
                                                           sampled_observations_dict['random'][r].tolist(),
                                                           sampled_scores_dict['random'][r].tolist(),
                                                           ground_truth,
                                                           num_classes=num_classes,
                                                           prior=UNIFORM_PRIOR)

            avg_num_agreement_dict['ts_uniform'][r], cumulative_metric_dict['ts_uniform'][r], \
            non_cumulative_metric_dict['ts_uniform'][r] = eval(args,
                                                               sampled_categories_dict['ts_uniform'][r].tolist(),
                                                               sampled_observations_dict['ts_uniform'][r].tolist(),
                                                               sampled_scores_dict['ts_uniform'][r].tolist(),
                                                               ground_truth,
                                                               num_classes=num_classes,
                                                               prior=UNIFORM_PRIOR)

            avg_num_agreement_dict['ts_informed'][r], cumulative_metric_dict['ts_informed'][r], \
            non_cumulative_metric_dict['ts_informed'][r] = eval(args,
                                                                sampled_categories_dict['ts_informed'][r].tolist(),
                                                                sampled_observations_dict['ts_informed'][r].tolist(),
                                                                sampled_scores_dict['ts_informed'][r].tolist(),
                                                                ground_truth,
                                                                num_classes=num_classes,
                                                                prior=INFORMED_PRIOR)

        for method in ['random', 'ts_uniform', 'ts_informed']:
            np.save(args.output / experiment_name / ('avg_num_agreement_%s.npy' % method),
                    avg_num_agreement_dict[method])
            np.save(args.output / experiment_name / ('cumulative_metric_%s.npy' % method),
                    cumulative_metric_dict[method])
            np.save(args.output / experiment_name / ('non_cumulative_metric_%s.npy' % method),
                    non_cumulative_metric_dict[method])
    else:
        for method in ['random', 'ts_uniform', 'ts_informed']:
            avg_num_agreement_dict[method] = np.load(
                args.output / experiment_name / ('avg_num_agreement_%s.npy' % method))
            cumulative_metric_dict[method] = np.load(
                args.output / experiment_name / ('cumulative_metric_%s.npy' % method))
            non_cumulative_metric_dict[method] = np.load(
                args.output / experiment_name / ('non_cumulative_metric_%s.npy' % method))

    if PLOT:
        comparison_plot(args, experiment_name, avg_num_agreement_dict, cumulative_metric_dict,
                        non_cumulative_metric_dict)


def main_calibration_error_topk(args: argparse.Namespace, SAMPLE=True, EVAL=True, PLOT=True) -> None:
    num_classes = num_classes_dict[args.dataset]

    categories, observations, confidences, idx2category, category2idx = prepare_data(datafile_dict[args.dataset], False)
    num_samples = len(observations)

    experiment_name = '%s_%s_%s_top%d_runs%d' % (args.dataset, args.metric, args.mode, args.topk, RUNS)

    if not (args.output / experiment_name).is_dir():
        (args.output / experiment_name).mkdir()

    sampled_categories_dict = {
        'random': np.empty((RUNS, num_samples), dtype=int),
        'ts': np.empty((RUNS, num_samples), dtype=int),
    }
    sampled_observations_dict = {
        'random': np.empty((RUNS, num_samples), dtype=bool),
        'ts': np.empty((RUNS, num_samples), dtype=bool),
    }
    sampled_scores_dict = {
        'random': np.empty((RUNS, num_samples), dtype=float),
        'ts': np.empty((RUNS, num_samples), dtype=float),
    }

    avg_num_agreement_dict = {
        'random': np.zeros((RUNS, num_samples // LOG_FREQ)),
        'ts': np.zeros((RUNS, num_samples // LOG_FREQ)),
    }
    cumulative_metric_dict = {
        'random': np.zeros((RUNS, num_samples // LOG_FREQ)),
        'ts': np.zeros((RUNS, num_samples // LOG_FREQ)),
    }
    non_cumulative_metric_dict = {
        'random': np.zeros((RUNS, num_samples // LOG_FREQ)),
        'ts': np.zeros((RUNS, num_samples // LOG_FREQ)),
    }

    if SAMPLE:
        for r in tqdm(range(RUNS)):
            sampled_categories_dict['random'][r], sampled_observations_dict['random'][r], sampled_scores_dict['random'][
                r] = get_samples_topk(args,
                                      categories,
                                      observations,
                                      confidences,
                                      num_classes,
                                      num_samples,
                                      sample_method='random',
                                      prior=None,
                                      random_seed=r)
            sampled_categories_dict['ts'][r], sampled_observations_dict['ts'][r], \
            sampled_scores_dict['ts'][r] = get_samples_topk(args,
                                                            categories,
                                                            observations,
                                                            confidences,
                                                            num_classes,
                                                            num_samples,
                                                            sample_method='ts',
                                                            prior=None,
                                                            random_seed=r)
        # write samples to file
        for method in ['random', 'ts']:
            np.save(args.output / experiment_name / ('sampled_categories_%s.npy' % method),
                    sampled_categories_dict[method])
            np.save(args.output / experiment_name / ('sampled_observations_%s.npy' % method),
                    sampled_observations_dict[method])
            np.save(args.output / experiment_name / ('sampled_scores_%s.npy' % method), sampled_scores_dict[method])
    else:
        # load sampled categories, scores and observations from file
        for method in ['random', 'ts']:
            sampled_categories_dict[method] = np.load(
                args.output / experiment_name / ('sampled_categories_%s.npy' % method))
            sampled_observations_dict[method] = np.load(
                args.output / experiment_name / ('sampled_observations_%s.npy' % method))
            sampled_scores_dict[method] = np.load(args.output / experiment_name / ('sampled_scores_%s.npy' % method))

    if EVAL:
        ground_truth = _get_ground_truth(categories, observations, confidences, num_classes, args.metric, args.mode,
                                         topk=args.topk)
        for r in tqdm(range(RUNS)):
            avg_num_agreement_dict['random'][r], cumulative_metric_dict['random'][r], \
            non_cumulative_metric_dict['random'][r] = eval(args,
                                                           sampled_categories_dict['random'][r].tolist(),
                                                           sampled_observations_dict['random'][r].tolist(),
                                                           sampled_scores_dict['random'][r].tolist(),
                                                           ground_truth,
                                                           num_classes=num_classes,
                                                           prior=None)

            avg_num_agreement_dict['ts'][r], cumulative_metric_dict['ts'][r], \
            non_cumulative_metric_dict['ts'][r] = eval(args,
                                                       sampled_categories_dict['ts'][r].tolist(),
                                                       sampled_observations_dict['ts'][r].tolist(),
                                                       sampled_scores_dict['ts'][r].tolist(),
                                                       ground_truth,
                                                       num_classes=num_classes,
                                                       prior=None)

        for method in ['random', 'ts']:
            np.save(args.output / experiment_name / ('avg_num_agreement_%s.npy' % method),
                    avg_num_agreement_dict[method])
            np.save(args.output / experiment_name / ('cumulative_metric_%s.npy' % method),
                    cumulative_metric_dict[method])
            np.save(args.output / experiment_name / ('non_cumulative_metric_%s.npy' % method),
                    non_cumulative_metric_dict[method])
    else:
        for method in ['random', 'ts']:
            avg_num_agreement_dict[method] = np.load(
                args.output / experiment_name / ('avg_num_agreement_%s.npy' % method))
            cumulative_metric_dict[method] = np.load(
                args.output / experiment_name / ('cumulative_metric_%s.npy' % method))
            non_cumulative_metric_dict[method] = np.load(
                args.output / experiment_name / ('non_cumulative_metric_%s.npy' % method))

    if PLOT:
        comparison_plot(args, experiment_name, avg_num_agreement_dict, cumulative_metric_dict,
                        non_cumulative_metric_dict)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('dataset', type=str, default='cifar100', help='input dataset')
    parser.add_argument('-output', type=pathlib.Path, default=OUTPUT_DIR, help='output prefix')
    parser.add_argument('-topk', type=int, default=10, help='number of optimal arms to identify')
    parser.add_argument('-mode', type=str, help='min or max, identify topk with highest/lowest reward')
    parser.add_argument('-metric', type=str, help='accuracy or calibration_error')
    args, _ = parser.parse_known_args()

    logging.basicConfig(level=logging.INFO)
    if args.dataset not in DATASET_LIST:
        raise ValueError("%s is not in DATASET_LIST." % args.dataset)

    print(args.dataset, args.mode, '...')
    if args.metric == 'accuracy':
        main_accuracy_topk(args, SAMPLE=True, EVAL=True, PLOT=True)
    elif args.metric == 'calibration_error':
        main_calibration_error_topk(args, SAMPLE=False, EVAL=False, PLOT=True)
