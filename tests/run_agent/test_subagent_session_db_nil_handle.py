import os
import tempfile
import unittest
from pathlib import Path

from run_agent import AIAgent
from hermes_state import SessionDB


def _make_subagent():
    return AIAgent(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_db=None,
        platform="subagent",
    )


class TestSubagentSessionDBNilHandle(unittest.TestCase):
    """Issue #723: subagent sessions must open state.db if no handle was provided."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".hermes"
        self.home.mkdir(parents=True, exist_ok=True)
        self._orig_hermes_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)

    def tearDown(self):
        if self._orig_hermes_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._orig_hermes_home
        self.tmp.cleanup()

    def test_subagent_session_opens_canonical_db(self):
        """A subagent without session_db lazily opens canonical state.db on first use."""
        agent = _make_subagent()
        self.assertIsNone(agent._session_db)
        agent._ensure_db_session()
        self.assertIsInstance(agent._session_db, SessionDB)
        self.assertTrue(agent._session_db_created)

        # Row exists and is tagged as a subagent child of the parent session.
        agent._flush_messages_to_session_db(
            [{"role": "user", "content": "hello"}], conversation_history=[]
        )
        row = agent._session_db.get_session(agent.session_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "subagent")

    def test_nil_handle_with_persist_disabled_does_not_open(self):
        """Background-review forks must not open canonical state.db."""
        agent = _make_subagent()
        agent._persist_disabled = True
        agent._ensure_db_session()
        self.assertIsNone(agent._session_db)
        self.assertFalse(agent._session_db_created)

    def test_existing_session_db_is_preserved(self):
        """When a SessionDB is passed in, it is still used unchanged."""
        db = SessionDB(self.home / "state.db")
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.com/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            platform="subagent",
        )
        self.assertIs(agent._session_db, db)
        agent._ensure_db_session()
        self.assertIs(agent._session_db, db)
        self.assertTrue(agent._session_db_created)


if __name__ == "__main__":
    unittest.main()
