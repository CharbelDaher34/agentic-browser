"""Eval suite (pydantic_evals).

Each Case is a goal + a checkable success criterion. The task function runs the
agent end-to-end against a throwaway session and returns the final state; custom
Evaluators score it. Run this in CI on every prompt/model/provider change — a
non-deterministic agent has no other ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from ..agent import AgentDeps, agent
from ..config import CoreConfig
from ..providers import make_provider
from ..recorder import Recorder
from ..registry import SessionRegistry
from ..session import PlaywrightSession
from .store_sql import Store


@dataclass
class Goal:
    instruction: str
    start_url: str


@dataclass
class Outcome:
    final_text: str
    final_url: str


class ReachedUrl(Evaluator[Goal, Outcome]):
    """Pass if the agent ended on a URL containing `needle`."""

    needle: str

    def evaluate(self, ctx: EvaluatorContext[Goal, Outcome]) -> float:
        return 1.0 if self.needle in ctx.output.final_url else 0.0


class Mentions(Evaluator[Goal, Outcome]):
    """Pass if the final answer mentions `needle` (cheap text check)."""

    needle: str

    def evaluate(self, ctx: EvaluatorContext[Goal, Outcome]) -> float:
        return 1.0 if self.needle.lower() in ctx.output.final_text.lower() else 0.0


async def _noop_emit(_):  # evals don't stream to a UI
    return None


def build_task(store: Store, recorder: Recorder, registry: SessionRegistry):
    async def task(goal: Goal) -> Outcome:
        sid = f"eval-{id(goal)}"
        await registry.create(sid, make_provider(cfg=CoreConfig()).name)  # fresh, isolated
        session: PlaywrightSession = registry.get(sid)
        await session._page.goto(goal.start_url)
        lease = await registry.acquire(sid, "agent", sid)
        deps = AgentDeps(
            sid, sid, lease.token, registry, recorder, _noop_emit, _idx=[0],
            cfg=CoreConfig(),
        )
        result = await agent.run(goal.instruction, deps=deps)
        out = Outcome(
            final_text=str(result.output),
            final_url=(await session.observe()).url,
        )
        await registry.release(sid, lease.token)
        await session.close()
        return out

    return task


def suite() -> Dataset[Goal, Outcome]:
    return Dataset[Goal, Outcome](
        cases=[
            Case(
                name="hn_top_story",
                inputs=Goal(
                    "Open Hacker News and tell me the top story title.",
                    "https://news.ycombinator.com",
                ),
                evaluators=[ReachedUrl(needle="ycombinator")],
            ),
            Case(
                name="wiki_search",
                inputs=Goal(
                    "Search Wikipedia for the Eiffel Tower and report its height.",
                    "https://en.wikipedia.org",
                ),
                evaluators=[Mentions(needle="metres")],
            ),
        ]
    )


async def run_evals(store: Store, recorder: Recorder, registry: SessionRegistry):
    report = await suite().evaluate(build_task(store, recorder, registry))
    report.print()          # table of pass/fail, scores, durations
    return report
