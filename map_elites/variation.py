import time

import numpy as np
import torch
import copy

from utils.logger import log
from utils.vectorized import BatchMLP
from utils.signal_slot import EventLoopObject, EventLoopProcess, signal, Timer


class VariationOperator(EventLoopObject):
    def __init__(self,
                 cfg,
                 all_actors,
                 elites_map,
                 eval_cache,
                 eval_in_queue,
                 free_policy_keys,
                 event_loop,
                 object_id,
                 crossover_op='iso_dd',
                 mutation_op=None,
                 max_gene=False,
                 min_gene=False,
                 mutation_rate=0.05,
                 crossover_rate=0.75,
                 eta_m=5.0,
                 eta_c=10.0,
                 sigma=0.1,
                 max_uniform=0.1,
                 iso_sigma=0.005,
                 line_sigma=0.05
                 ):
        super().__init__(event_loop, object_id)
        self.cfg = cfg
        self.all_actors = all_actors
        self.elites_map = elites_map
        self.eval_cache = eval_cache
        self.eval_in_queue = eval_in_queue
        self.free_policy_keys = free_policy_keys
        self.init_mode = True
        self.queued_for_eval = 0  # want to always keep some mutated policies queued so that the evaluator(s) are never waiting

        # variation hyperparams
        if cfg.crossover_op in ["sbx", "iso_dd"]:
            self.crossover_op = getattr(self, cfg.crossover_op)
        else:
            log.warn("Not using the crossover operator")
            self.crossover_op = False

        if cfg.mutation_op in ['polynomial_mutation', 'gaussian_mutation', 'uniform_mutation']:
            self.mutation_op = getattr(self, cfg.mutation_op)
        else:
            log.warn("Not using the mutation operator")
            self.mutation_op = False
        self.max = max_gene
        self.min = min_gene
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.eta_m = eta_m
        self.eta_c = eta_c
        self.sigma = sigma
        self.max_uniform = max_uniform
        self.iso_sigma = iso_sigma
        self.line_sigma = line_sigma

        def periodic(period, callback):
            return Timer(self.event_loop, period).timeout.connect(callback)

        periodic(3.0, self.maybe_mutate_new_batch)

    @signal
    def to_evaluate(self): pass

    @signal
    def stop(self): pass

    def on_stop(self):
        self.stop.emit(self.object_id)
        if isinstance(self.event_loop.process, EventLoopProcess):
            self.event_loop.stop()
        self.detach()

    def on_eval_results(self, oid, agents, evaluated_actors_keys, frames, runtime, avg_ep_length):
        num_eval = len(agents)
        self.queued_for_eval -= num_eval
        log.debug(f'Received {len(agents)} processed agents from {oid}, {self.queued_for_eval=}')

    def on_release(self, mapped_actors_keys):
        self.free_policy_keys.update(mapped_actors_keys)

    def maybe_mutate_new_batch(self):
        if self.queued_for_eval < 2 * self.cfg.num_agents:
            init_mode = True if len(self.elites_map) <= self.cfg.random_init else False
            self.evolve_batch(init_mode)

    def evolve_batch(self, init=True):
        '''
        Mutate a new batch of policies
        :param init: whether the elites map still needs to be initialized with random policies or not
        '''
        if init:  # initialize archive with random policies
            log.debug('Initializing the map of elites with random policies')
            batch_size = self.cfg.random_init_batch
            keys = list(self.free_policy_keys)  # policy keys
        else:  # get policies from the archive and mutate those
            log.debug('Mutating policies from the map of elites')
            batch_size = int(self.cfg['eval_batch_size'] * self.cfg['proportion_evo'])
            keys = [x[0] for x in self.elites_map.values()]

        free_keys = list(self.free_policy_keys & set(keys))
        if len(free_keys) < batch_size:
            log.warn(f'Warning: not enough free policies available to mutate, {len(free_keys)=}, {batch_size=}. '
                     f'Variation worker will skip this iteration of mutations. Consider increasing the initial size of '
                     f'the elites map.')
            return
        actor_x_ids = []
        actor_y_ids = [None for _ in range(len(actor_x_ids))]

        if self.mutation_op and not self.crossover_op:
            actor_x_ids = np.random.choice(free_keys, size=batch_size, replace=False)
            actor_x_ids = np.repeat(actor_x_ids, repeats=self.cfg.mutations_per_policy)

        elif self.crossover_op:
            actor_x_ids = np.random.choice(free_keys, size=batch_size, replace=False)
            actor_y_ids = np.random.choice(free_keys, size=batch_size, replace=False)
            actor_x_ids = np.repeat(actor_x_ids, repeats=self.cfg.mutations_per_policy)
            actor_y_ids = np.repeat(actor_y_ids, repeats=self.cfg.mutations_per_policy)

        # remove the keys we will use from the set of all available policy keys
        self.free_policy_keys -= set(actor_x_ids)

        actors_x_evo = self.all_actors[actor_x_ids][:, 0]
        actors_y_evo = self.all_actors[actor_y_ids][:, 0] if actor_y_ids is not None else None

        device = torch.device('cpu')  # evolve NNs on the cpu, keep gpu memory for batch inference in eval workers
        batch_actors_x = BatchMLP(actors_x_evo, device)
        batch_actors_y = BatchMLP(actors_y_evo, device) if actors_y_evo is not None else None
        with torch.no_grad():
            actor_z = self.evo(batch_actors_x, batch_actors_y, device, self.crossover_op, self.mutation_op)
            actors_z = actor_z.update_mlps(copy.deepcopy(actors_x_evo))

        # place in eval cache for Evaluator to evaluate
        unique_actor_x_ids = list(set(actor_x_ids))
        cached_actors = self.eval_cache[unique_actor_x_ids]  # get unique locations in eval_cache
        actors_z = actors_z.reshape(-1, self.cfg.mutations_per_policy)  # b/c we repeated each policy key M times
        for p_cache, pz in zip(cached_actors, actors_z):  # p_cache and pz are lists of mlps
            for cached_policy, mutated_policy in zip(p_cache, pz):
                cached_policy.load_state_dict(mutated_policy.state_dict())
        self.queued_for_eval += len(actor_x_ids)
        self.eval_in_queue.put(unique_actor_x_ids)
        self.to_evaluate.emit(self.object_id, self.init_mode)

    def flush(self, q):
        '''
        Flush the remaining contents of a q
        :param q: any standard Queue() object
        '''
        while not q.empty():
            q.get_many()



    def evo(self, actor_x, actor_y, device, crossover_op=None, mutation_op=None):
        '''
        evolve the agent parameters (in this case, a neural network) using crossover and mutation operators
        crossover needs 2 parents, thus actor_x and actor_y
        mutation can just be performed on actor_x to get actor_z (child)
        '''
        start_time = time.time()
        actor_z = copy.deepcopy(actor_x)
        actor_z.type = 'evo'
        actor_z_state_dict = actor_z.state_dict()
        if crossover_op:
            actor_x_ids = actor_x.get_mlp_ids()
            actor_y_ids = actor_y.get_mlp_ids()
            # TODO: figure out another way to keep track of evolutionary history
            # actor_z.set_parent_id(which_parent=1, ids=actor_x_ids)
            # actor_z.set_parent_id(which_parent=2, ids=actor_y_ids)
            actor_z_state_dict = self.batch_crossover(actor_x.state_dict(), actor_y.state_dict(), crossover_op, device=device)
            if mutation_op:
                actor_z_state_dict = self.batch_mutation(actor_z_state_dict, mutation_op)
        elif mutation_op:
            actor_x_ids = actor_x.get_mlp_ids()
            # actor_z.set_parent_id(which_parent=1, ids=actor_x_ids)
            actor_z_state_dict = self.batch_mutation(actor_z_state_dict, mutation_op)

        actor_z.load_state_dict(actor_z_state_dict)
        runtime = time.time() - start_time
        log.debug(f'Mutated {len(actor_x)} actors in {runtime:.1f} seconds')
        return actor_z

    def batch_crossover(self, batch_x_state_dict, batch_y_state_dict, crossover_op, device):
        """
        perform crossover operation b/w two parents (actor x and actor y) to produce child (actor z) for a batch of parents
        :return: a BatchMLP object containing all the actors_z resulting from the crossover of x/y actors
        """
        batch_z_state_dict = copy.deepcopy(batch_x_state_dict)
        for tensor in batch_x_state_dict:
            if 'weight' or 'bias' in tensor:
                batch_z_state_dict[tensor] = crossover_op(batch_x_state_dict[tensor], batch_y_state_dict[tensor]).to(device)
        return batch_z_state_dict

    def batch_mutation(self, batch_x_state_dict, mutation_op):
        y = copy.deepcopy(batch_x_state_dict)
        for tensor in batch_x_state_dict:
            if 'weight' or 'bias' in tensor:
                y[tensor] = mutation_op(batch_x_state_dict[tensor])
        return y

    def iso_dd(self, x, y):
        '''
        Iso+Line
        Ref:
        Vassiliades V, Mouret JB. Discovering the elite hypervolume by leveraging interspecies correlation.
        GECCO 2018
        Support for batch processing
        '''
        with torch.no_grad():
            a = torch.zeros_like(x).normal_(mean=0, std=self.iso_sigma)
            b = np.random.normal(0, self.line_sigma)
            z = x.clone() + a + b * (y - x)

        if not self.max and not self.min:
            return z
        else:
            with torch.no_grad():
                return torch.clamp(z, self.min, self.max)


    ################################
    # Mutation Operators ###########
    ################################

    def gaussian_mutation(self, x):
        """
        Mutate the params of the agents x according to a gaussian
        """
        with torch.no_grad():
            y = x.clone()
            m = torch.rand_like(y)
            index = torch.where(m < self.mutation_rate)
            delta = torch.zeros(index[0].shape).normal_(mean=0, std=self.sigma).to(y.device)
            if len(y.shape) == 1:
                y[index[0]] += delta
            elif len(y.shape) == 2:
                y[index[0], index[1]] += delta
            else:
                # 3D i.e. BatchMLP
                y[index[0], index[1], index[2]] += delta
            if not self.max and not self.min:
                return y
            else:
                return torch.clamp(y, self.min, self.max)