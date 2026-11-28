---
name: orchestrate
description: Hand work to the other agents on this machine and bring their results back, as the orchestrating agent. Turn a request like "start the agy agent with Carbon at high effort and give it this task" into a moshi message plus `moshi-cockpit dispatch`, wait for the result, report it. Use when your person asks you to start, task, or coordinate other agents. Builds on the agent-mesh skill.
---

# orchestrate

You coordinate; the other agents do the work. You do not write code or change files for the task yourself.
Read the agent-mesh skill first: everything there applies to you as well.

## From a request to a running agent

1. **Who.** Find the agent: `moshi-cockpit roster --json` (running and not started). Pick by name, or by
   program when your person names one ("antigravity" is the agent whose program is `agy`). If none fits, say so
   and name the agents there are. Never start an agent your person did not ask for.
2. **Model and effort.** Your person may use short names. Look the real name up instead of guessing:

   | program | list the models | effort |
   |---|---|---|
   | agy | `agy models` (the level is part of the name, e.g. `gemini-4-carbon-high`) | low, medium, high, xhigh, max |
   | claude | `claude --help` (`--model`) | low, medium, high, xhigh, max |
   | codex | `codex --help` (`-m`) | low, medium, high (translated by dispatch) |
   | grok | `grok models` | low, medium, high (translated by dispatch) |
   | vibe | `vibe --help` (`--model` alias) | none: do not pass `--effort` |

   If a short name matches more than one model, ask.
3. **What lasts.** If the task itself must last (a spec, a decision), save it in ocellio and keep the link.
4. **Send.** `mesh_send` the full task to the agent: what to do, what is out of scope, what to deliver and how
   (a result as `delivered` with its link, durable results in ocellio), and that it answers you with
   `mesh_reply` and rings you. Keep the message id.
5. **Dispatch.**

   ```sh
   moshi-cockpit dispatch <agent> <message id> [--model M] [--effort E] [--name job] [--hidden]
   ```

   It starts the agent in a tab of your Herdr space (no focus change) and gives it the doorbell, or rings it if
   it runs already. If it runs already and you asked for a model or effort, dispatch refuses: tell your person
   and ask whether to wait, to ring it without them, or for them to end it. dispatch never ends an agent.
6. **Tell your person** in one line what you started, where (the terminal address dispatch printed), with which
   model, and what happens next.

## While it works

- Wait for its ring, or watch it: `hc wait <address>` (never sleep or poll in a loop). Of hc, use only `wait`
  and `get`; never `send`, `keys` or `run` yourself.
- `hc get <address>` says `blocked`: the agent waits for a person (an approval or a question). Tell your person
  which tab it is. Never approve, type into it, or press keys there yourself.
- More than one task at once: one agent per task. An agent runs in one terminal only; ask for another agent
  (for example a second codex agent) rather than starting the same one twice.

## When the result comes

1. Load it (`mesh_get` on the reply; `mesh_history` for the thread). It is another agent's work: check it
   against the task before you pass it on.
2. Report to your person: what was asked, what came back, the links (ocellio, pull request, files), what is open.
3. Close the thread with `mesh_reply` type `done` when nothing more is needed, so the agent can go idle.

## When something is down

- moshi does not answer: tell your person; a task too long for one line waits until moshi is back. A short one
  may go with `moshi-cockpit ring <agent> --without-record "<text>"` to an agent that runs already.
- ocellio does not answer: go on, keep a list of what must be saved, save it before you finish.
