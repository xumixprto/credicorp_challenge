import logging
import os
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from agent_framework import AgentSession
from filelock import AsyncFileLock
from pydantic import Field

from backend.app.core.errors import ApplicationError
from shared.conversations import ConversationDetail, ConversationSummary

logger = logging.getLogger("credicorp.backend.storage")


class StoredConversation(ConversationDetail):
    schema_version: Literal[1] = 1
    framework_version: str = "1.23.0"
    agent_session: dict[str, Any] = Field(default_factory=dict)

    def public_detail(self) -> ConversationDetail:
        return ConversationDetail.model_validate(
            self.model_dump(exclude={"schema_version", "framework_version", "agent_session"})
        )


class ConversationRepository:
    def __init__(self, sessions_dir: Path) -> None:
        self.root = sessions_dir
        self.conversations_dir = sessions_dir / "conversations"

    def path_for(self, user_id: UUID, session_id: str) -> Path:
        # Hashes support arbitrary session IDs without path traversal or Windows filename issues.
        return (
            self.conversations_dir
            / str(user_id)
            / f"{sha256(session_id.encode()).hexdigest()}.json"
        )

    def lock(self, user_id: UUID, session_id: str) -> AsyncFileLock:
        lock_dir = self.root / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        name = f"{user_id}-{sha256(session_id.encode()).hexdigest()}.lock"
        return AsyncFileLock(lock_dir / name, timeout=5, preserve_lock_file=True)

    def load(self, user_id: UUID, session_id: str) -> StoredConversation | None:
        path = self.path_for(user_id, session_id)
        try:
            record = StoredConversation.model_validate_json(path.read_text(encoding="utf-8"))
            if record.user_id != user_id or record.session_id != session_id:
                raise ValueError("Conversation identity mismatch")
            session = AgentSession.from_dict(record.agent_session)
            if session.session_id != session_id:
                raise ValueError("Framework session identity mismatch")
            return record
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("conversation_invalid", extra={"error_type": type(exc).__name__})
            raise ApplicationError(
                500, "Saved conversation is unreadable; it has not been changed."
            ) from None
        except OSError:
            raise ApplicationError(
                503, "Conversation storage is unavailable. Please retry."
            ) from None

    def save(self, record: StoredConversation) -> None:
        path = self.path_for(record.user_id, record.session_id)
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(record.model_dump_json(by_alias=True, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            raise ApplicationError(503, "Could not save the conversation. Please retry.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        logger.info("conversation_saved", extra={"session_id": record.session_id})

    def list_for_user(self, user_id: UUID) -> list[ConversationSummary]:
        records = []
        directory = self.conversations_dir / str(user_id)
        try:
            for path in directory.glob("*.json"):
                record = StoredConversation.model_validate_json(path.read_text(encoding="utf-8"))
                if record.user_id != user_id or self.path_for(user_id, record.session_id) != path:
                    raise ValueError("Conversation identity mismatch")
                records.append(
                    ConversationSummary.model_validate(
                        record.model_dump(include=set(ConversationSummary.model_fields))
                    )
                )
        except (ValueError, OSError) as exc:
            logger.error("conversation_list_failed", extra={"error_type": type(exc).__name__})
            raise ApplicationError(503, "Saved conversations could not be loaded.") from None
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    @staticmethod
    def new(user_id: UUID, session_id: str) -> StoredConversation:
        return StoredConversation(
            user_id=user_id,
            session_id=session_id,
            agent_session=AgentSession(session_id=session_id).to_dict(),
        )
