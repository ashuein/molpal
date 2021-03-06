"""This module contains the Explorer class, which is an abstraction
for batched, Bayesian optimization."""

from collections import deque
import csv
import heapq
from itertools import zip_longest
import os
from operator import itemgetter
from pathlib import Path
import pickle
import tempfile
from typing import Dict, List, Optional, Tuple, TypeVar, Union

from molpal import acquirer, encoder, models, objectives, pools

T = TypeVar('T')

class Explorer:
    """An Explorer explores a pool of inputs using Bayesian optimization

    Attributes
    ----------
    name : str
        the name this explorer will use for all outputs
    pool : MoleculePool
        the pool of inputs to explore
    encoder : Encoder
        the encoder this explorer will use convert molecules from SMILES
        strings into feature representations
    acquirer : Acquirer
        an acquirer which selects molecules to explore next using a prior
        distribution over the inputs
    objective : Objective
        an objective calculates the objective function of a set of inputs
    model : Model
        a model that generates a posterior distribution over the inputs using
        observed data
    retrain_from_scratch : bool
        whether the model will be retrained from scratch at each iteration.
        If False, train the model online. 
        NOTE: The definition of 'online' is model-specific.
    epoch : int
        the current epoch of exploration
    scores : Dict[T, float]
        a dictionary mapping an input's identifier to its corresponding
        objective function value
    failed : Dict[T, None]
        a dictionary containing the inputs for which the objective function
        failed to evaluate
    new_scores : Dict[T, float]
        a dictionary mapping an input's identifier to its corresponding
        objective function value for the most recent batch of labeled inputs
    updated_model : bool
        whether the predictions are currently out-of-date with the model
    top_k_avg : float
        the average of the top-k explored inputs
    y_preds : List[float]
        a list parallel to the pool containing the mean predicted score
        for an input
    y_vars : List[float]
        a list parallel to the pool containing the variance in the predicted
        score for an input. Will be empty if model does not provide variance
    recent_avgs : Deque[float]
        a queue containing the <window_size> most recent averages
    delta : float
        the minimum acceptable fractional difference between the current 
        average and the moving average in order to continue exploration
    max_epochs : int
        the maximum number of batches to explore
    root : str
        the directory under which to organize all outputs
    write_final : bool
        whether the list of explored inputs and their scores should be written
        to a file at the end of exploration
    write_intermediate : bool
        whether the list of explored inputs and their scores should be written
        to a file after each round of exploration
    scores_csvs : List[str]
        a list containing the filepath of each score file that was written
        in the order in which they were written. Used only when saving the
        intermediate state to initialize another explorer
    write_preds : bool
        whether the predictions should be written after each exploration batch
    verbose : int
        the level of output the Explorer prints

    Parameters
    ----------
    name : str
    k : Union[int, float] (Default = 0.01)
    window_size : int (Default = 3)
        the number of top-k averages from which to calculate a moving average
    delta : float (Default = 0.01)
    max_epochs : int (Default = 50)
    max_explore : Union[int, float] (Default = 1.)
    root : str (Default = '.')
    write_final : bool (Default = True)
    write_intermediate : bool (Default = False)
    save_preds : bool (Default = False)
    retrain_from_scratch : bool (Default = False)
    previous_scores : Optional[str] (Default = None)
        the filepath of a CSV file containing previous scoring data which will
        be treated as the initialization batch (instead of randomly selecting
        from the bool.)
    scores_csvs : Union[str, List[str], None] (Default = None)
        a list of filepaths containing CSVs with previous scoring data or a 
        pickle file containing this list. These
        CSVs will be read in and the model trained on the data in the order
        in which the CSVs are provide. This is useful for mimicking the
        intermediate state of a previous Explorer instance
    verbose : int (Default = 0)
    **kwargs
        keyword arguments to initialize an Encoder, MoleculePool, Acquirer, 
        Model, and Objective classes

    Raises
    ------
    ValueError
        if k is less than 0
        if max_explore is less than 0
    """
    def __init__(self, name: str = 'molpal',
                 k: Union[int, float] = 0.01, window_size: int = 3,
                 delta: float = 0.01, max_epochs: int = 50, 
                 max_explore: Union[int, float] = 1., root: str = '.',
                 write_final: bool = True, write_intermediate: bool = False,
                 save_preds: bool = False, retrain_from_scratch: bool = False,
                 previous_scores: Optional[str] = None,
                 scores_csvs: Union[str, List[str], None] = None,
                 verbose: int = 0, **kwargs):
        self.name = name; kwargs['name'] = name
        self.verbose = verbose; kwargs['verbose'] = verbose
        self.root = root
        self.tmp = tempfile.gettempdir()

        self.encoder = encoder.Encoder(**kwargs)
        self.pool = pools.pool(encoder=self.encoder, 
                               path=tempfile.gettempdir(), **kwargs)
        self.acquirer = acquirer.Acquirer(size=len(self.pool), **kwargs)

        if self.acquirer.metric == 'thompson':
            kwargs['dropout_size'] = 1
        self.model = models.model(input_size=len(self.encoder), **kwargs)
        self.acquirer.stochastic_preds = 'stochastic' in self.model.provides

        self.objective = objectives.objective(**kwargs)

        self._validate_acquirer()
        
        self.retrain_from_scratch = retrain_from_scratch

        self.k = k
        self.delta = delta
        self.max_explore = max_explore
        self.max_epochs = max_epochs

        self.write_final = write_final
        self.write_intermediate = write_intermediate
        self.save_preds = save_preds

        # stateful attributes (not including model)
        self.epoch = 0
        self.scores = {}
        self.failures = {}
        self.new_scores = {}
        self.updated_model = None
        self.recent_avgs = deque(maxlen=window_size)
        self.top_k_avg = None
        self.y_preds = None
        self.y_vars = None

        if isinstance(scores_csvs, str):
            self.scores_csvs = pickle.load(open(scores_csvs, 'rb'))
        elif isinstance(scores_csvs, list):
            self.scores_csvs = scores_csvs
        else:
            self.scores_csvs = []

        if previous_scores:
            self.load_scores(previous_scores)
        elif scores_csvs:
            self.load()

    @property
    def k(self) -> int:
        """int : The number of top-scoring inputs from which to determine
        the average."""
        k = self.__k
        if isinstance(k, float):
            k = int(k * len(self.pool))
            
        return min(k, len(self.pool))

    @k.setter
    def k(self, k: Union[int, float]):
        """Set k either as an integer or as a fraction of the pool.
        
        NOTE: Specifying either a fraction greater than 1 or or a number 
        larger than the pool size will default to using the full pool.
        """
        if k <= 0:
            raise ValueError(f'k(={k}) must be greater than 0!')
        self.__k = k

    @property
    def max_explore(self) -> int:
        """int : The maximum number of inputs to explore"""
        max_explore = self.__max_explore
        if isinstance(max_explore, float):
            max_explore = int(max_explore * len(self.pool))
        
        return max_explore
    
    @max_explore.setter
    def max_explore(self, max_explore: Union[int, float]):
        """Set max_explore either as an integer or as a fraction of the pool.
        
        NOTE: Specifying either a fraction greater than 1 or or a number 
        larger than the pool size will default to using the full pool.
        """
        if max_explore <= 0.:
            raise ValueError(
                f'max_explore(={max_explore}) must be greater than 0!')

        self.__max_explore = max_explore

    @property
    def completed(self) -> bool:
        """Has the explorer fulfilled one of its stopping conditions?

        Stopping Conditions
        -------------------
        a. explored the entire pool
           (not implemented right now due to complications with 'transfer 
           learning')
        b. explored for at least <max_epochs> epochs
        c. explored at least <max_explore> inputs
        d. the current top-k average is within a fraction <delta> of the moving
           top-k average. This requires two sub-conditions to be met:
           1. the explorer has successfully explored at least k inputs
           2. the explorer has completed at least <window_size> epochs after
              sub-condition (1) has been met

        Returns
        -------
        bool
            whether a stopping condition has been met
        """
        if self.epoch > self.max_epochs:
            return True
        if len(self.scores) >= self.max_explore:
            return True

        if len(self.recent_avgs) < self.recent_avgs.maxlen:
            return False

        moving_avg = sum(self.recent_avgs) / len(self.recent_avgs)
        return (self.top_k_avg - moving_avg) / moving_avg <= self.delta

    def explore(self):
        self.run()

    def run(self):
        """Explore the MoleculePool until the stopping condition is met"""
        
        if self.epoch == 0:
            print('Starting Exploration ...')
            self.explore_initial()
        else:
            print(f'Resuming Exploration at epoch {self.epoch}...')
            self.explore_batch()

        while not self.completed:
            if self.verbose > 0:
                print(f'Current average of top {self.k}: {self.top_k_avg:0.3f}',
                      'Continuing exploration ...', flush=True)
            self.explore_batch()

        print('Finished exploring!')
        print(f'Explored a total of {len(self)} molecules',
              f'over {self.epoch} iterations')
        print(f'Final average of top {self.k}: {self.top_k_avg:0.3f}')
        print(f'Final averages')
        print(f'--------------')
        for k in [0.0001, 0.0005, 0.001, 0.005]:
            print(f'top {k*100:0.2f}%: {self.avg(k):0.3f}')
        
        if self.write_final:
            self.write_scores(final=True)

    def __len__(self) -> int:
        """The number of inputs that have been explored"""
        return len(self.scores) + len(self.failures)

    def explore_initial(self) -> float:
        """Perform an initial round of exploration
        
        Must be called before explore_batch()

        Returns
        -------
        avg : float
            the average score of the batch
        """
        inputs = self.acquirer.acquire_initial(
            xs=self.pool.smis(),
            cluster_ids=self.pool.cluster_ids(),
            cluster_sizes=self.pool.cluster_sizes,
        )

        new_scores = self.objective.calc(
            inputs,
            in_path=f'{self.tmp}/{self.name}/inputs/iter_{self.epoch}',
            out_path=f'{self.tmp}/{self.name}/outputs/iter_{self.epoch}'
        )
        self._clean_and_update_scores(new_scores)

        self.top_k_avg = self.avg()
        if len(self.scores) >= self.k:
            self.recent_avgs.append(self.top_k_avg)

        if self.write_intermediate:
            self.write_scores(include_failed=True)
        
        self.epoch += 1

        valid_scores = [y for y in new_scores.values() if y is not None]
        return sum(valid_scores)/len(valid_scores)

    def explore_batch(self) -> float:
        """Perform a round of exploration

        Returns
        -------
        avg : float
            the average score of the batch

        Raises
        ------
        InvalidExplorationError
            if called before explore_initial or load_scores
        """
        if self.epoch == 0:
            raise InvalidExplorationError(
                'Cannot explore a batch before initialization!')

        if len(self.scores) >= len(self.pool):
            # this needs to be reconsidered for transfer learning type approach
            self.epoch += 1
            return self.top_k_avg

        self._update_model()
        self._update_predictions()

        inputs = self.acquirer.acquire_batch(
            xs=self.pool.smis(), y_means=self.y_preds, y_vars=self.y_vars,
            explored={**self.scores, **self.failures},
            cluster_ids=self.pool.cluster_ids(),
            cluster_sizes=self.pool.cluster_sizes, epoch=self.epoch,
        )

        new_scores = self.objective.calc(
            inputs,
            in_path=f'{self.tmp}/{self.name}/inputs/iter_{self.epoch}',
            out_path=f'{self.tmp}/{self.name}/outputs/iter_{self.epoch}'
        )
        self._clean_and_update_scores(new_scores)

        self.top_k_avg = self.avg()
        if len(self.scores) >= self.k:
            self.recent_avgs.append(self.top_k_avg)

        if self.write_intermediate:
            self.write_scores(include_failed=True)
        
        self.epoch += 1

        valid_scores = [y for y in new_scores.values() if y is not None]
        return sum(valid_scores)/len(valid_scores)

    def avg(self, k: Union[int, float, None] = None) -> float:
        """Calculate the average of the top k molecules
        
        Parameter
        ---------
        k : Union[int, float, None] (Default = None)
            the number of molecules to consider when calculating the
            average, expressed either as a specific number or as a 
            fraction of the pool. If the value specified is greater than the 
            number of successfully evaluated inputs, return the average of all 
            succesfully evaluated inputs. If None, use self.k
        
        Returns
        -------
        float
            the top-k average
        """
        k = k or self.k
        if isinstance(k, float):
            k = int(k * len(self.pool))
        k = min(k, len(self.scores))

        if k == len(self.pool):
            return sum(score for score in self.scores.items()) / k
        
        return sum(score for smi, score in self.top_explored(k)) / k

    def top_explored(self, k: Union[int, float, None] = None) -> List[Tuple]:
        """Get the top-k explored molecules
        
        Parameter
        ---------
        k : Union[int, float, None] (Default = None)
            the number of top-scoring molecules to get, expressed either as a 
            specific number or as a fraction of the pool. If the value 
            specified is greater than the number of successfully evaluated 
            inputs, return all explored inputs. If None, use self.k
        
        Returns
        -------
        top_explored : List[Tuple[str, float]]
            a list of tuples containing the identifier and score of the 
            top-k inputs, sorted by their score
        """
        k = k or self.k
        if isinstance(k, float):
            k = int(k * len(self.pool))
        k = min(k, len(self.scores))

        if k / len(self.scores) < 0.8:
            return heapq.nlargest(k, self.scores.items(), key=itemgetter(1))
        
        return sorted(self.scores.items(), key=itemgetter(1), reverse=True)

    def top_preds(self, k: Union[int, float, None] = None) -> List[Tuple]:
        """Get the current top predicted molecules and their scores
        
        Parameter
        ---------
        k : Union[int, float, None] (Default = None)
            see documentation for avg()
        
        Returns
        -------
        top_preds : List[Tuple[str, float]]
            a list of tuples containing the identifier and predicted score of 
            the top-k predicted inputs, sorted by their predicted score
        """
        k = k or self.k
        if isinstance(k, float):
            k = int(k * len(self.pool))
        k = min(k, len(self.scores))

        selected = []
        for x, y in zip(self.pool.smis(), self.y_preds):
            if len(selected) < k:
                heapq.heappush(selected, (y, x))
            else:
                heapq.heappushpop(selected, (y, x))

        return [(x, y) for y, x in selected]

    def write_scores(self, m: Union[int, float] = 1., 
                     final: bool = False,
                     include_failed: bool = False) -> None:
        """Write the top M scores to a CSV file

        Writes a CSV file of the top-k explored inputs with the input ID and
        the respective objective function value.

        Parameters
        ----------
        m : Union[int, float] (Default = 1.)
            The number of top-scoring inputs to write, expressed either as an
            integer or as a float representing the fraction of explored inputs.
            By default, writes all inputs
        final : bool (Default = False)
            Whether the explorer has finished. If true, write all explored
            inputs (both successful and failed) and name the output CSV file
            "all_explored_final.csv"
        include_failed : bool (Default = False)
            Whether to include the inputs for which objective function
            evaluation failed
        """
        if isinstance(m, float):
            m = int(m * len(self))
        m = min(m, len(self))

        p_data = Path(f'{self.root}/{self.name}/data')
        if not p_data.is_dir():
            p_data.mkdir(parents=True)

        if final:
            m = len(self)
            p_scores = p_data / f'all_explored_final.csv'
            include_failed = True
        else:
            p_scores = p_data / f'top_{m}_explored_iter_{self.epoch}.csv'
        self.scores_csvs.append(str(p_scores))

        top_m = self.top_explored(m)

        with open(p_scores, 'w') as fid:
            writer = csv.writer(fid)
            writer.writerow(['smiles', 'score'])
            writer.writerows(top_m)
            if include_failed:
                writer.writerows(self.failures.items())
        
        if self.verbose > 0:
            print(f'Results were written to "{p_scores}"')

    def load_scores(self, previous_scores: str) -> None:
        """Load the scores CSV located at saved_scores.
        
        If this is being called during initialization, treat the data as the
        initialization batch.

        Parameter
        ---------
        previous_scores : str
            the filepath of a CSV file containing previous scoring information.
            The 0th column of this CSV must contain the input identifier and
            the 1st column must contain a float corresponding to its score.
            A failure to parse the 1st column as a float will treat that input
            as a failure.
        """
        if self.verbose > 0:
            print(f'Loading scores from "{previous_scores}" ... ', end='')

        scores, failures = self._read_scores(previous_scores)
        self.scores.update(scores)
        self.failures.update(failures)
        
        if self.epoch == 0:
            self.epoch = 1
        
        if self.verbose > 0:
            print('Done!')

    def save(self) -> str:
        p_states = Path(f'{self.root}/{self.name}/states')
        if not p_states.is_dir():
            p_states.mkdir(parents=True)
        
        p_state = p_states / f'epoch_{self.epoch}.pkl'
        with open(p_state, 'wb') as fid:
            pickle.dump(self.scores_csvs, fid)

        return str(p_state)

    def load(self) -> None:
        """Mimic the intermediate state of a previous explorer run by loading
        the data from the list of output files"""

        if self.verbose > 0:
            print(f'Loading in previous state ... ', end='')

        for scores_csv in self.scores_csvs:
            scores, self.failures = self._read_scores(scores_csv)

            self.new_scores = {smi: score for smi, score in scores.items()
                               if smi not in self.scores}

            if not self.retrain_from_scratch:
                self._update_model()

            self.scores = scores
            self.epoch += 1

            self.top_k_avg = self.avg()
            if len(self.scores) >= self.k:
                self.recent_avgs.append(self.top_k_avg)

        if self.verbose > 0:
            print('Done!')

    def write_preds(self) -> None:
        preds_path = Path(f'{self.root}/{self.name}/preds')
        if not preds_path.is_dir():
            preds_path.mkdir(parents=True)

        with open(f'{preds_path}/preds_iter_{self.epoch}.csv', 'w') as fid:
            writer = csv.writer(fid)
            writer.writerow(
                ['smiles', 'predicted_score', '[predicted_variance]']
            )
            writer.writerows(
                zip_longest(self.pool.smis(), self.y_preds, self.y_vars)
            )
    
    def _clean_and_update_scores(self, new_scores: Dict[T, Optional[float]]):
        """Remove the None entries from new_scores and update the attributes 
        new_scores, scores, and failed accordingly

        Parameter
        ---------
        new_scores : Dict[T, Optional[float]]
            a dictionary containing the corresponding values of the objective
            function for a batch of inputs

        Side effects
        ------------
        (mutates) self.scores : Dict[T, float]
            updates self.scores with the non-None entries from new_scores
        (mutates) self.new_scores : Dict[T, float]
            updates self.new_scores with the non-None entries from new_scores
        (mutates) self.failures : Dict[T, None]
            a dictionary storing the inputs for which scoring failed
        """
        for x, y in new_scores.items():
            if y is None:
                self.failures[x] = y
            else:
                self.scores[x] = y
                self.new_scores[x] = y

    def _update_model(self) -> None:
        """Update the prior distribution to generate a posterior distribution

        Side effects
        ------------
        (mutates) self.model : Type[Model]
            updates the model with new data, if there are any
        (sets) self.new_scores : Dict[str, Optional[float]]
            reinitializes self.new_scores to an empty dictionary
        (sets) self.updated_model : bool
            sets self.updated_model to True, indicating that the predictions
            must be updated as well
        """
        if len(self.new_scores) == 0:
            # only update model if there are new data
            self.updated_model = False
            return

        if self.retrain_from_scratch:
            xs, ys = zip(*self.scores.items())
        else:
            xs, ys = zip(*self.new_scores.items())

        self.model.train(xs, ys, retrain=self.retrain_from_scratch,
                         featurize=self.encoder.encode_and_uncompress)
        self.new_scores = {}
        self.updated_model = True

    def _update_predictions(self) -> None:
        """Update the predictions over the pool with the new model

        Side effects
        ------------
        (sets) self.y_preds : List[float]
            a list of floats parallel to the pool inputs containing the mean
            predicted score for each input
        (sets) self.y_vars : List[float]
            a list of floats parallel to the pool inputs containing the
            predicted variance for each input
        (sets) self.updated_model : bool
            sets self.updated_model to False, indicating that the predictions 
            are now up-to-date with the current model
        """
        if not self.updated_model and self.y_preds:
            # don't update predictions if the model has not been updated 
            # and the predictions are already set
            return

        self.y_preds, self.y_vars = self.model.apply(
            x_ids=self.pool.smis(), 
            x_feats=self.pool.fps(), 
            batched_size=None, size=len(self.pool), 
            mean_only='vars' not in self.acquirer.needs
        )

        self.updated_model = False
        
        if self.save_preds:
            self.write_preds()

    def _validate_acquirer(self):
        """Ensure that the model provides values the Acquirer needs"""
        if self.acquirer.needs > self.model.provides:
            raise IncompatibilityError(
                f'{self.acquirer.metric} metric needs: '
                + f'{self.acquirer.needs} '
                + f'but {self.model.type_} only provides: '
                + f'{self.model.provides}')

    def _read_scores(self, scores_csv: str) -> Dict:
        """read the scores contained in the file located at scores_csv"""
        scores = {}
        failures = {}
        with open(scores_csv) as fid:
            reader = csv.reader(fid)
            next(reader)
            for row in reader:
                try:
                    scores[row[0]] = float(row[1])
                except:
                    failures[row[0]] = None
        
        return scores, failures

class InvalidExplorationError(Exception):
    pass

class IncompatibilityError(Exception):
    pass
