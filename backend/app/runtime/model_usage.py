"""Account for main, delegated and tool-internal model responses in the owning run."""

from langchain_core.callbacks import AsyncCallbackHandler


class RunUsageCallback(AsyncCallbackHandler):
    raise_error = True

    def __init__(self, tracker, guard) -> None:
        self.tracker = tracker
        self.guard = guard

    async def on_llm_end(self, response, **kwargs) -> None:
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is not None:
                    self.tracker.add(message)
        self.guard.check(self.tracker)
