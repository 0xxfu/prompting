# The MIT License (MIT)
# Copyright © 2023 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.


import time
import torch
import bittensor as bt

from prompting.forward import forward
from prompting.llm import load_pipeline
from prompting.base.validator import BaseValidatorNeuron
from prompting.rewards import RewardPipeline


class Validator(BaseValidatorNeuron):
    """
    Text prompt validator neuron.
    """

    def __init__(self, config=None):
        super(Validator, self).__init__(config=config)

        bt.logging.info("load_state()")
        self.load_state()

        self.llm_pipeline = load_pipeline(
            model_id=self.config.model_id,
            torch_dtype=torch.bfloat16,
            device_map=self.device,
            mock=self.config.mock,
        )

        if sum(self.config.neuron.task_p) != 1:
            raise ValueError("Task probabilities do not sum to 1.")

        # Filter out tasks with 0 probability
        self.active_tasks = [
            task
            for task, p in zip(
                self.config.neuron.tasks, self.config.neuron.task_p
            )
            if p > 0
        ]
        # Load the reward pipeline
        self.reward_pipeline = RewardPipeline(selected_tasks=self.active_tasks)
        bt.logging.error(f'Reward pipeline: {self.reward_pipeline}')

    async def forward(self):
        """
        Validator forward pass. Consists of:
        - Generating the query
        - Querying the miners
        - Getting the responses
        - Rewarding the miners
        - Updating the scores
        """
        return await forward(self)


# The main function parses the configuration and runs the validator.
if __name__ == "__main__":
    with Validator() as validator:
        while True:
            bt.logging.info("Validator running...", time.time())
            time.sleep(5)
