"""The History LLM API: read-only recorder tools, gated on Assist exposure.

Data shaping (bucketing, time parsing, table layout) lives in shaping.py so it
can be unit-tested without Home Assistant; this module only fetches and gates.
"""

from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, override

import probatio

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.components.recorder import get_instance, history, statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from . import shaping
from .const import DOMAIN

API_PROMPT = (
    "History tools read recorded data for entities exposed to Assist. Call "
    "list_entities to find entity_ids, get_states for detailed recent history, "
    "and get_statistics for long-range hourly/daily/monthly figures such as daily "
    "kWh. All times in results are local ISO 8601."
)

MAX_ENTITIES = 10
MAX_STAT_ROWS = 2000
PERIOD_SECONDS = {"5minute": 300, "hour": 3600, "day": 86400, "week": 604800,
                  "month": 2592000}
STAT_TYPES = ["mean", "min", "max", "change", "sum", "state"]
TIME_HELP = ("'now', a duration ago like '90m', '24h', '7d', '1y', or an ISO "
             "date/datetime (no offset = local time)")

READ_ONLY = llm.ToolAnnotations(
    read_only=True, destructive=False, idempotent=True, open_world=False
)


def _entity_ids_key():
    return probatio.Required(
        "entity_ids", description=f"Entity ids to read (1-{MAX_ENTITIES})."
    )


def _window_keys(default_start: str) -> dict:
    return {
        probatio.Optional("start", description=f"Window start: {TIME_HELP}. "
                          f"Default '{default_start}'."): str,
        probatio.Optional("end", description=f"Window end: {TIME_HELP}. "
                          "Default now."): str,
    }


def _error(message: str) -> llm.ToolResult:
    return llm.ToolResult(data={"error": message}, error=True)


def _split_exposed(hass: HomeAssistant, assistant: str, entity_ids: list[str]):
    """(readable ids, {id: reason}) - unknown and unexposed ids are refused."""
    allowed, errors = [], {}
    for entity_id in dict.fromkeys(e.strip().lower() for e in entity_ids):
        if hass.states.get(entity_id) is None:
            errors[entity_id] = "unknown entity"
        elif not async_should_expose(hass, assistant, entity_id):
            errors[entity_id] = "not exposed to Assist"
        else:
            allowed.append(entity_id)
    return allowed, errors


def _check_entity_count(entity_ids: list[str]) -> str | None:
    if not 1 <= len(entity_ids) <= MAX_ENTITIES:
        return f"entity_ids must list 1-{MAX_ENTITIES} entities, got {len(entity_ids)}"
    return None


def _window(args: dict, default_start: str):
    tz = dt_util.get_default_time_zone()
    start, end = shaping.resolve_window(
        args.get("start"), args.get("end"), dt_util.now(), tz, default_start
    )
    return tz, start, end


class ListEntitiesTool(llm.Tool):
    """Discover which entities history can be read for."""

    name = "list_entities"
    title = "List entities with history"
    description = (
        "List entities whose history can be read (those exposed to Assist): "
        "entity_id, name, unit, current state, and whether long-term statistics "
        "exist (needed for get_statistics). Call first to find entity_ids."
    )
    parameters = probatio.Schema({
        probatio.Optional("search", description="Case-insensitive text to match "
                          "in the entity_id or name."): str,
    })
    annotations = READ_ONLY
    integration = DOMAIN

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> llm.ToolResult:
        args = self.parameters(tool_input.tool_args)
        needle = args.get("search", "").strip().lower()
        rows = []
        for state in hass.states.async_all():
            if not async_should_expose(hass, llm_context.assistant, state.entity_id):
                continue
            if needle and needle not in state.entity_id and needle not in state.name.lower():
                continue
            rows.append({
                "entity_id": state.entity_id,
                "name": state.name,
                "state": state.state,
                "unit": state.attributes.get("unit_of_measurement"),
                "statistics": "state_class" in state.attributes,
            })
        rows.sort(key=lambda r: r["entity_id"])
        return llm.ToolResult(data={"count": len(rows), "entities": rows})


class GetStatesTool(llm.Tool):
    """Recorder state history, bucketed for charts."""

    name = "get_states"
    title = "Get state history"
    parameters = probatio.Schema({
        _entity_ids_key(): [str],
        **_window_keys("24h"),
        probatio.Optional("max_points", default=200, description="Target rows per "
                          "series (10-1000); numeric buckets are sized to fit "
                          "(one extra when the start is clock-aligned)."):
            probatio.All(int, probatio.Range(min=10, max=1000)),
    })
    annotations = READ_ONLY
    integration = DOMAIN

    def __init__(self, keep_days: int) -> None:
        """Describe the recorder's actual retention."""
        self.keep_days = keep_days
        self.description = (
            f"Detailed state history from the recorder (last {keep_days} days only). "
            "Numeric entities: rows of [bucket_start, mean, min, max], where mean is "
            "time-weighted and the bucket size (bucket_seconds) is chosen to fit "
            "max_points. Text and on/off entities: rows of [time, state] at each "
            "change, newest kept. For older data or daily/monthly totals use "
            "get_statistics."
        )

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> llm.ToolResult:
        args = self.parameters(tool_input.tool_args)
        if problem := _check_entity_count(args["entity_ids"]):
            return _error(problem)
        try:
            tz, start, end = _window(args, "24h")
        except ValueError as err:
            return _error(str(err))
        allowed, errors = _split_exposed(hass, llm_context.assistant, args["entity_ids"])
        # Numeric buckets sit on local clock boundaries (12:00, 12:30, midnight),
        # so the window is widened back to the first boundary before fetching.
        bucket = shaping.pick_bucket(end.timestamp() - start.timestamp(),
                                     args["max_points"])
        start = datetime.fromtimestamp(
            shaping.align_start(start.timestamp(), bucket, tz), tz)

        found = {}
        if allowed:
            found = await get_instance(hass).async_add_executor_job(partial(
                history.get_significant_states, hass, start, end, allowed,
                include_start_time_state=True, significant_changes_only=False,
                no_attributes=True,
            ))

        series = []
        for entity_id in allowed:
            points = [(s.last_changed_timestamp, s.state) for s in found.get(entity_id, [])]
            current = hass.states.get(entity_id)
            item: dict[str, Any] = {
                "entity_id": entity_id,
                "name": current.name if current else entity_id,
                "unit": current.attributes.get("unit_of_measurement") if current else None,
            }
            if shaping.classify([p[1] for p in points]) == "numeric":
                rows = shaping.bucket_numeric(
                    [(ts, shaping.to_number(v)) for ts, v in points],
                    start.timestamp(), end.timestamp(), bucket,
                )
                item.update(kind="numeric", bucket_seconds=bucket,
                            columns=["start", "mean", "min", "max"])
            else:
                rows, truncated = shaping.text_changes(points, args["max_points"])
                item.update(kind="text", columns=["time", "state"], truncated=truncated)
            item["rows"] = shaping.format_rows(rows, tz)
            series.append(item)

        data: dict[str, Any] = {
            "start": start.astimezone(tz).isoformat(timespec="seconds"),
            "end": end.astimezone(tz).isoformat(timespec="seconds"),
            "series": series,
        }
        if (dt_util.now() - start).days >= self.keep_days:
            data["note"] = (f"The recorder keeps {self.keep_days} days of state history; "
                            "use get_statistics for anything older.")
        if errors:
            data["errors"] = errors
        return llm.ToolResult(data=data)


class GetStatisticsTool(llm.Tool):
    """Long-term (hourly and coarser) statistics, kept indefinitely."""

    name = "get_statistics"
    title = "Get long-term statistics"
    description = (
        "Long-term statistics for entities with a state_class (see list_entities), "
        "kept indefinitely. period: 5minute (recent days only), hour, day, week, "
        "month - day/week/month follow local time. types: mean/min/max for "
        "measurements (power, temperature, SoC); change for counters, i.e. the "
        "amount per period such as daily kWh; also sum, state. Default: change for "
        "counters, mean/min/max otherwise. Rows: [period_start, ...types]."
    )
    parameters = probatio.Schema({
        _entity_ids_key(): [str],
        **_window_keys("7d"),
        probatio.Optional("period", default="hour", description="Aggregation period."):
            probatio.In(list(PERIOD_SECONDS)),
        probatio.Optional("types", description="Statistic types to return, in order. "
                          "Omit for the per-entity default."): [probatio.In(STAT_TYPES)],
    })
    annotations = READ_ONLY
    integration = DOMAIN

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> llm.ToolResult:
        args = self.parameters(tool_input.tool_args)
        if problem := _check_entity_count(args["entity_ids"]):
            return _error(problem)
        try:
            tz, start, end = _window(args, "7d")
        except ValueError as err:
            return _error(str(err))
        period = args["period"]
        expected = (end - start).total_seconds() / PERIOD_SECONDS[period]
        if expected > MAX_STAT_ROWS:
            return _error(f"{period} rows over this window would be ~{int(expected)} "
                          f"per entity (max {MAX_STAT_ROWS}); use a coarser period "
                          "or a shorter window")
        allowed, errors = _split_exposed(hass, llm_context.assistant, args["entity_ids"])

        recorder = get_instance(hass)
        meta = {}
        if allowed:
            meta = await recorder.async_add_executor_job(partial(
                statistics.get_metadata, hass, statistic_ids=set(allowed)))
        types_for = {}
        for entity_id in allowed:
            if entity_id not in meta:
                errors[entity_id] = "no long-term statistics (entity has no state_class)"
                continue
            m = meta[entity_id][1]
            has_mean = bool(m.get("mean_type")) or bool(m.get("has_mean"))
            types_for[entity_id] = args.get("types") or shaping.default_stat_types(
                has_sum=m["has_sum"], has_mean=has_mean)

        found = {}
        if types_for:
            found = await recorder.async_add_executor_job(
                statistics.statistics_during_period, hass, start, end,
                set(types_for), period, None,
                {t for types in types_for.values() for t in types},
            )

        series = []
        for entity_id, types in types_for.items():
            current = hass.states.get(entity_id)
            series.append({
                "entity_id": entity_id,
                "name": current.name if current else entity_id,
                "unit": meta[entity_id][1].get("unit_of_measurement"),
                "period": period,
                **shaping.statistics_table(found.get(entity_id, []), types, tz),
            })

        data: dict[str, Any] = {
            "start": start.astimezone(tz).isoformat(timespec="seconds"),
            "end": end.astimezone(tz).isoformat(timespec="seconds"),
            "series": series,
        }
        if errors:
            data["errors"] = errors
        return llm.ToolResult(data=data)


@dataclass(slots=True, kw_only=True)
class HistoryAPI(llm.API):
    """LLM API exposing the history tools."""

    @override
    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        """Return the instance of the API."""
        return llm.APIInstance(
            api=self,
            api_prompt=API_PROMPT,
            llm_context=llm_context,
            tools=[
                ListEntitiesTool(),
                GetStatesTool(get_instance(self.hass).keep_days),
                GetStatisticsTool(),
            ],
        )
