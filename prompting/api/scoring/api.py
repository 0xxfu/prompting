import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from loguru import logger

from prompting.datasets.random_website import DDGDatasetEntry
from prompting.llms.model_zoo import ModelZoo
from prompting.rewards.scoring import task_scorer
from prompting.tasks.inference import InferenceTask
from prompting.tasks.web_retrieval import WebRetrievalTask
from shared.epistula import verify_signature
from shared.base import DatasetEntry
from shared.dendrite import DendriteResponseEvent
from shared.epistula import SynapseStreamResult
from shared.settings import shared_settings

router = APIRouter()

def verify_scoring_signature(
    signature: str,
    body: bytes,
    signed_by: str,
) -> Optional[Annotated[str, "Error Message"]]:
    # Basic type checks
    if not isinstance(signature, str):
        return "Invalid signature type"
    if not isinstance(body, bytes):
        return "Body is not of type bytes"
    if not isinstance(signed_by, str):
        return "Invalid 'signed_by' address"

    # Check that the 'signed_by' matches the expected SS58 address
    if signed_by != shared_settings.API_HOTKEY:
        return "Message not from the expected SS58 address"

    # Build the message to verify
    message = f"{sha256(body).hexdigest()}.{uuid}.{timestamp}.{signed_for}"

    # Create a keypair using the 'signed_by' address
    keypair = Keypair(ss58_address=signed_by)

    # Verify the signature
    verified = keypair.verify(message, signature)
    if not verified:
        return "Signature mismatch"

    return None


@router.post("/scoring")
async def score_response(request: Request, api_key_data: dict = Depends(verify_signature)):
    model = None
    payload: dict[str, Any] = await request.json()
    body = payload.get("body")
    timeout = payload.get("timeout", shared_settings.NEURON_TIMEOUT)
    uids = payload.get("uid", [])
    chunks = payload.get("chunks", {})
    if not uids or not chunks:
        logger.error(f"Either uids: {uids} or chunks: {chunks} is not valid, skipping scoring")
        return
    uids = [int(uid) for uid in uids]
    model = body.get("model")
    if model:
        try:
            llm_model = ModelZoo.get_model_by_id(model)
        except Exception:
            logger.warning(
                f"Organic request with model {body.get('model')} made but the model cannot be found in model zoo. Skipping scoring."
            )
        return
    else:
        llm_model = None
    task_name = body.get("task")
    if task_name == "InferenceTask":
        logger.info(f"Received Organic InferenceTask with body: {body}")
        logger.info(f"With model of type {type(body.get('model'))}")
        organic_task = InferenceTask(
            messages=body.get("messages"),
            llm_model=llm_model,
            llm_model_id=body.get("model"),
            seed=int(body.get("seed", 0)),
            sampling_params=body.get("sampling_parameters", shared_settings.SAMPLING_PARAMS),
            query=body.get("messages"),
        )
        logger.info(f"Task created: {organic_task}")
        task_scorer.add_to_queue(
            task=organic_task,
            response=DendriteResponseEvent(
                uids=uids,
                stream_results=[SynapseStreamResult(accumulated_chunks=chunks.get(str(uid), None)) for uid in uids],
                timeout=timeout,
            ),
            dataset_entry=DatasetEntry(),
            block=shared_settings.METAGRAPH.block,
            step=-1,
            task_id=str(uuid.uuid4()),
        )
    elif task_name == "WebRetrievalTask":
        logger.info(f"Received Organic WebRetrievalTask with body: {body}")
        try:
            search_term = body.get("messages")[0].get("content")
        except Exception as ex:
            logger.error(f"Failed to get search term from messages: {ex}, can't score WebRetrievalTask")
            return

        task_scorer.add_to_queue(
            task=WebRetrievalTask(
                messages=[msg["content"] for msg in body.get("messages")],
                seed=int(body.get("seed", 0)),
                sampling_params=body.get("sampling_params", {}),
                query=search_term,
            ),
            response=DendriteResponseEvent(
                uids=[uids],
                stream_results=[
                    SynapseStreamResult(accumulated_chunks=[chunk for chunk in chunks if chunk is not None])
                ],
                timeout=body.get("timeout", shared_settings.NEURON_TIMEOUT),
            ),
            dataset_entry=DDGDatasetEntry(search_term=search_term),
            block=shared_settings.METAGRAPH.block,
            step=-1,
            task_id=str(uuid.uuid4()),
        )
    logger.info("Organic task appended to scoring queue")
