import io
import os
import operator
import sys
import json
import time

import pandas as pd
import numpy as np

from topmodel import hmetrics

THRESHOLD_BINS = 100

SCORES_FILE = 'scores.tsv'
ACTUALS_FILE = 'actuals.tsv'
SCORES_BM_FILE = 'scores_bm.tsv'

HISTOGRAM_FILE = 'histogram.json'
BOOTSTRAP_FILE = 'bootstrap.json'
NOTES_FILE = "notes.txt"
METADATA_FILE = "metadata.txt"

TOP_THRESHOLDS = [0.9, 0.95, 0.99, 0.995, 0.9975, 0.999, 0.9995,
                  0.9999, 0.99995, 0.999999, 0.9999999, 1]

BIN_COUNT = 100


class ModelDataManager(object):

    def __init__(self, file_system):
        self.file_system = file_system
        self.models = {}
        self.names_and_updated = {}

        all_models_with_times = self.file_system.list_name_modified().items()
        scores_hash, scores_bm_hash = self.get_hash_of_models(all_models_with_times)

        for filepath, time in scores_hash.items():
            basedir, _ = os.path.split(filepath)
            model_data = ModelData(self.file_system, basedir)
            self.models[basedir] = model_data
            self.names_and_updated[basedir] = time

        for filepath, time in scores_bm_hash.items():
            model_path, _ = os.path.split(filepath)
            model_data = BenchmarkedModelData(self.file_system, model_path)
            self.models[model_path] = model_data
            self.names_and_updated[model_path] = time

    def get_hash_of_models(self, all_models_with_times):
        scores_hash, actuals_hash, scores_bm_hash = {}, {}, {}

        for k, v in all_models_with_times:
            if k.endswith(SCORES_FILE):
                scores_hash[k] = v

        for k, v in all_models_with_times:
            if k.endswith(ACTUALS_FILE):
                actuals_hash[k] = v
        for path, time in actuals_hash.items():
            basedir = os.path.split(path)[0]
            for k, v in self.file_system.list_name_modified(basedir).items():
                if k.endswith(SCORES_BM_FILE):
                    scores_bm_hash[k] = v

        return scores_hash, scores_bm_hash

    def search(self, target_model):
        return filter(lambda model: target_model in model.model_path, self.list())


class ModelData(object):

    def __init__(self, file_system, model_path):
        self.model_path = model_path
        self.file_system = file_system
        self.data_frame = None

    def metrics_from_hist(self, hist):
        return {
            # facts about the histogram
            'thresholds': hist['thresholds'],
            'score_distribution': hist['totals'],
            'trues': hist['trues'],
            # 3 main metrics
            'precisions': hmetrics.precisions(hist),
            'recalls': hmetrics.recalls(hist),
            'fprs': hmetrics.fprs(hist),
            # extra metrics
            'marginal_precisions': hmetrics.marginal_precisions(hist),
            # single number metrics
            'logloss': hmetrics.logloss(hist)
        }

    def get_metrics(self, n_bootstrap_samples=0):
        hist = self.to_histogram_format(resample=False)
        base = self.metrics_from_hist(hist)

        if n_bootstrap_samples == 0:
            return base
        else:
            bootstrapped = self.to_bootstrap_format(n_bootstrap_samples)
            return [base] + bootstrapped

    def to_bootstrap_format(self, n_bootstrap_samples):
        bootstrap_path = os.path.join(self.model_path, BOOTSTRAP_FILE)
        bootstrap_json = self.file_system.read_file(bootstrap_path)

        if bootstrap_json is None or len(json.loads(bootstrap_json)) != n_bootstrap_samples:
            bootstrap = []
            for _ in xrange(n_bootstrap_samples):
                resampled_hist = self.to_histogram_format(resample=True)
                bootstrap.append(self.metrics_from_hist(resampled_hist))

            self.file_system.write_file(bootstrap_path, json.dumps(bootstrap))

        else:
            bootstrap = json.loads(bootstrap_json)

        return bootstrap

    def get_top_metrics(self):
        hist = self.to_histogram_format(resample=False)
        return self.metrics_from_hist(hist['high_end_hist'])

    def check_alt_format(self):
        # alternate data format is "score,trues,falses"
        # here we build the DataFrame to match the old scores.tsv
        # weights are not supported in the alternate format.

        orig_df = self.data_frame
        if 'trues' in orig_df.columns:
            true_df = pd.DataFrame(
                data={'actual': np.repeat(True, len(orig_df)),
                      'weight': orig_df['trues'],
                      'pred_score': orig_df['score']})
            false_df = pd.DataFrame(
                data={'actual': np.repeat(False, len(orig_df)),
                      'weight': orig_df['falses'],
                      'pred_score': orig_df['score']})
            self.data_frame = pd.concat([true_df, false_df])

    def to_data_frame(self, **kwargs):
        if self.data_frame is None:
            scores_path = os.path.join(self.model_path, SCORES_FILE)
            csv = self.file_system.read_file(scores_path)
            with io.BytesIO(csv) as f:
                self.data_frame = pd.read_csv(f, sep='\t', **kwargs)
            self.data_frame = self.data_frame.dropna(how='any')

        self.check_alt_format()
        return self.data_frame

    def get_thresholds_trues_totals(self, range_info, bin_list, predicted, actual, weight):
        trues, totals, thresholds = [], [], []
        for i in range(range_info):
            thresholds.append(bin_list[i + 1])
            obs_in_bin = (predicted >= bin_list[i]) & (predicted < bin_list[i + 1])
            true_obs_in_bin = obs_in_bin & actual
            trues.append(np.sum(weight * true_obs_in_bin))
            totals.append(np.sum(weight * obs_in_bin))

        return {'thresholds': thresholds, 'trues': trues, 'totals': totals}

    def to_histogram_format(self, resample=False):
        # Build histogram of the data quantized to THRESHOLD_BINS bins
        # that's a O(1) size representation
        # If resample is True, sample the data frame with replacement before making histogram.
        histogram_path = os.path.join(self.model_path, HISTOGRAM_FILE)
        histogram_json = self.file_system.read_file(histogram_path)
        # This will force a recalculation of the histogram using the new key names
        # and ensure that each page will have a top thresholds view.
        if histogram_json is None or resample or 'high_end_hist' not in histogram_json:
            df = self.to_data_frame()
            if resample:
                # resample all rows of data frame with replacement
                df = df.iloc[np.random.randint(0, len(df), len(df))]
            actual = df.get('actual')
            predicted = df.get('pred_score')
            if df.get('weight') is None:
                weight = np.ones(len(df))
            else:
                weight = df['weight']
            bin_edges = map(
                lambda x: x * 1.0 / THRESHOLD_BINS, range(0, THRESHOLD_BINS + 1))

            top_bins = TOP_THRESHOLDS[:]
            top_bins.insert(0, 0.0)

            ret = self.get_thresholds_trues_totals(THRESHOLD_BINS, bin_edges, predicted, actual, weight)

            # If it's not a resample, calculate the top thresholds and cache the histogram.
            if not resample:
                high_end = self.get_thresholds_trues_totals(len(TOP_THRESHOLDS), top_bins, predicted, actual, weight)
                ret['high_end_hist'] = high_end

                self.file_system.write_file(histogram_path, json.dumps(ret))

            # Return the histogram bins + corresponding "true" and "total" counts
            return ret

        else:
            return json.loads(histogram_json)

    def save_data_frame(self, df):
        self.data_frame = df
        with io.BytesIO() as f:
            df.to_csv(f, sep='\t', index=False)
            scores_path = os.path.join(self.model_path, SCORES_FILE)
            self.file_system.write_file(scores_path, f.getvalue())

    def get_metadata(self):
        metadata_path = os.path.join(self.model_path, METADATA_FILE)
        return self.file_system.read_file(metadata_path)

    def get_notes(self):
        notes_path = os.path.join(self.model_path, NOTES_FILE)
        return self.file_system.read_file(notes_path)

    def set_notes(self, note):
        notes_path = os.path.join(self.model_path, NOTES_FILE)
        return self.file_system.write_file(notes_path, note)


class BenchmarkedModelData(ModelData):
    """
    Benchmarked model data. Store actuals in a separate 'actual.tsv' file
    with observations identifiers in an 'id' column and actuals in an
    'actual' column. Upload scores in a 'scores_bm.tsv' file that has
    a matching 'id' column' and 'pred_scores' column. Throws an error
    if the scores and actuals do not completely line up.
    """
    def indexed_data_frame(self, path, **kwargs):
        raw = self.file_system.read_file(path)
        with io.BytesIO(raw) as f:
            df = pd.read_csv(f, sep='\t', index_col=False, **kwargs)
        assert df.duplicated('id').sum() == 0, "id column is not unique"
        return df.set_index('id')

    def to_data_frame(self, **kwargs):
        if self.data_frame is None:
            basedir, _ = os.path.split(self.model_path)
            actuals_path = os.path.join(basedir, ACTUALS_FILE)
            df_actuals = self.indexed_data_frame(actuals_path, **kwargs)
            scores_path = os.path.join(self.model_path, SCORES_BM_FILE)
            df_scores = self.indexed_data_frame(scores_path, **kwargs)
            assert sorted(df_actuals.index) == sorted(df_scores.index), \
                "Indices for actuals and scores do not match"

            self.data_frame = pd.merge(
                df_actuals, df_scores, left_index=True, right_index=True)
            self.data_frame = self.data_frame.dropna(how='any')

        self.check_alt_format()
        return self.data_frame
