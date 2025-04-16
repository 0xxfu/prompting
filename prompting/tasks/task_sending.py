# ruff: noqa: E402
import asyncio
import time

from loguru import logger

from prompting.miner_availability.miner_availability import MinerAvailabilities

# from prompting.rewards.scoring import task_scorer
from prompting.rewards.scoring_config import ScoringConfig
from prompting.tasks.base_task import BaseTextTask
from prompting.tasks.inference import InferenceTask
from prompting.tasks.web_retrieval import WebRetrievalTask
from shared import settings
from shared.dendrite import DendriteResponseEvent
from shared.epistula import query_miners
from shared.logging import ErrorLoggingEvent, ValidatorLoggingEvent
from shared.loop_runner import AsyncLoopRunner
from shared.timer import Timer

shared_settings = settings.shared_settings

NEURON_SAMPLE_SIZE = 100


def log_stream_results(stream_results):
    failed_responses = [
        response for response in stream_results if response.exception is not None or response.completion is None
    ]
    empty_responses = [
        response for response in stream_results if response.exception is None and response.completion == ""
    ]
    non_empty_responses = [
        response for response in stream_results if response.exception is None and response.completion != ""
    ]

    logger.debug(f"Total of non_empty responses: ({len(non_empty_responses)})")
    logger.debug(f"Total of empty responses: ({len(empty_responses)})")
    logger.debug(f"Total of failed responses: ({len(failed_responses)})")


async def collect_responses(task: BaseTextTask, miners_dict: dict) -> DendriteResponseEvent | None:
    # Get the list of uids and their axons to query for this step.
    uids = MinerAvailabilities.get_available_miners(
        miners=miners_dict, task=task, model=task.llm_model_id, k=NEURON_SAMPLE_SIZE
    )
    if len(uids) == 0:
        logger.warning("No available miners. This should already have been caught earlier.")
        return

    body = {
        "seed": task.seed,
        "sampling_parameters": task.sampling_params,
        "task": task.__class__.__name__,
        "model": task.llm_model_id,
        "messages": task.task_messages,
    }
    if isinstance(task, WebRetrievalTask):
        body["target_results"] = task.target_results
    body["timeout"] = task.timeout
    stream_results = await query_miners(uids, body, timeout_seconds=task.timeout)

    response_event = DendriteResponseEvent(
        stream_results=stream_results,
        uids=uids,
        axons=[
            shared_settings.METAGRAPH.axons[x].ip + ":" + str(shared_settings.METAGRAPH.axons[x].port) for x in uids
        ],
        # TODO: I think we calculate the timeout dynamically, so this is likely wrong
        timeout=(
            shared_settings.INFERENCE_TIMEOUT if isinstance(task, InferenceTask) else shared_settings.NEURON_TIMEOUT
        ),
    )
    return response_event


class TaskSender(AsyncLoopRunner):
    interval: int = 10
    _lock: asyncio.Lock = asyncio.Lock()
    block_sync_last_time: float = 0
    block_sync_interval: float = 300

    task_queue: list | None = None
    scoring_queue: list | None = None
    # subtensor: bt.Subtensor | None = None
    miners_dict: dict | None = None

    class Config:
        arbitrary_types_allowed = True

    async def start(self, task_queue, scoring_queue, miners_dict, **kwargs):
        self.task_queue = task_queue
        self.scoring_queue = scoring_queue
        self.miners_dict = miners_dict

        # # shared_settings is not initialised inside this process, meaning it cannot access any non-constants from here
        # self.subtensor = bt.subtensor(network=shared_settings.SUBTENSOR_NETWORK)
        return await super().start(**kwargs)

    @property
    def block(self) -> int:
        time_since_last_block = time.time() - self.block_sync_last_time
        if time_since_last_block > self.block_sync_interval:
            self._block = shared_settings.SUBTENSOR.get_current_block()
            self.block_sync_last_time = time.time()
            return self._block

        blocks_passed = time_since_last_block // 12
        return self._block + blocks_passed

    async def run_step(self) -> ValidatorLoggingEvent | ErrorLoggingEvent | None:
        logger.info("Checking for tasks to be sent...")
        while len(self.scoring_queue) > shared_settings.SCORING_QUEUE_LENGTH_THRESHOLD:
            await asyncio.sleep(10)

        while len(self.task_queue) == 0:
            await asyncio.sleep(10)

        try:
            task = self.task_queue.pop(0)
            # send the task to the miners and collect the responses
            with Timer(label=f"Sending {task.__class__.__name__}") as timer:
                response_event = await collect_responses(task=task, miners_dict=self.miners_dict)
            if response_event is None:
                return

            scoring_config = ScoringConfig(
                task=task,
                response=response_event,
                dataset_entry=task.dataset_entry,
                block=self.block,
                step=self.step,
                task_id=task.task_id,
            )
            self.scoring_queue.append(scoring_config)
            # logger.debug(f"Scoring queue length: {len(self.scoring_queue)}")

            # Log the step event.
            return ValidatorLoggingEvent(
                block=self.block,
                step=self.step,
                step_time=timer.final_time,
                response_event=response_event,
                task_id=task.task_id,
            )
        except Exception as ex:
            logger.exception(ex)
            return ErrorLoggingEvent(
                error=str(ex),
            )
