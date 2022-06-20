import torch
import torch.nn as nn
import copy
import time
import numpy as np
from models.policy import Policy
from utils.vectorized import BatchMLP


class AntNN(Policy):
    def __init__(self, input_dim=60, hidden_size=256, action_dim=8, init_type='xavier_uniform'):
        super().__init__()
        assert init_type in ['xavier_uniform', 'kaiming_uniform', 'orthogonal'], 'The provided initialization type is not supported'
        self.init_func = getattr(nn.init, init_type + '_')  # >.<

        # variables for map elites
        self.type = None
        self.id = None
        self.parent_1_id = None
        self.parent_2_id = None
        self.novel = None
        self.delta_f = None

        self.layers = nn.Sequential(
            nn.Linear(input_dim, hidden_size),
            nn.Tanh(),
            nn.Linear(hidden_size, hidden_size),
            nn.Tanh(),
            nn.Linear(hidden_size, action_dim),
            nn.Tanh()
        )
        self._action_log_std = nn.Parameter(torch.zeros(action_dim,))

    def forward(self, obs):
        return self.layers(obs)

    @property
    def action_log_std(self):
        return self._action_log_std

    @action_log_std.setter
    def action_log_std(self, log_stddev):
        self._action_log_std = log_stddev

    # currently only support for linear layers
    def init_weights(self, m, init_type='xavier_uniform'):
        if isinstance(m, nn.Linear):
            self.init_func(m.weight)

    def save(self, filename):
        torch.save(self.state_dict(), filename)

    def load(self, filename):
        self.load_state_dict(torch.load(filename, map_location=torch.device('cpu')))

    def return_copy(self):
        return copy.deepcopy(self)


def ant_model_factory(device, hidden_size=256, init_type='xavier_uniform', share_memory=True):
    model = torch.jit.script(AntNN(hidden_size=hidden_size, init_type=init_type).to(device))
    model.apply(model.init_weights)
    if share_memory:
        model.share_memory()
    return model



if __name__ == '__main__':
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    m = torch.jit.script(AntNN().to(device))
    m_nojit = AntNN().to(device)
    inputs = torch.randn((100, 60)).to(device)

    start = time.time()
    for _ in range(100000):
        m(inputs)
    finish = time.time() - start
    print(f'Jitted model took {finish} seconds to complete')

    start = time.time()
    for _ in range(100000):
        m_nojit(inputs)
    finish = time.time() - start
    print(f'No-jit model took {finish} seconds to complete')