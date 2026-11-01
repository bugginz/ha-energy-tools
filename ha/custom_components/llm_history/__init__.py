"""History tools for Home Assistant's LLM APIs, and so for its MCP server.

HA's built-in MCP server (mcp_server) only serves live state and Assist
controls. This registers an extra LLM API, "History", with read-only tools
over the recorder (state history, long-term statistics). Tick it alongside
"Assist" in the MCP server's options and the same /api/mcp endpoint, login
and Funnel paths serve both. Only entities exposed to Assist are readable.

Deploy (HA Container on the Pi, enabled by the `llm_history:` YAML key):

    rsync -a --exclude __pycache__ ha/custom_components/llm_history/ \
        robwil@192.168.1.52:/opt/stack/ha/config/custom_components/llm_history/
    ssh robwil@192.168.1.52 docker restart homeassistant
"""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, llm
from homeassistant.helpers.typing import ConfigType

from .api import HistoryAPI
from .const import API_NAME, DOMAIN

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the History LLM API."""
    llm.async_register_api(hass, HistoryAPI(hass=hass, id=DOMAIN, name=API_NAME))
    return True
