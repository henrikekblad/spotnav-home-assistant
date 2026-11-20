"""One-click config flow: the demo entry is created without asking anything."""

from homeassistant.config_entries import ConfigFlow

from . import DOMAIN


class DemoConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=DOMAIN, data={})
