from trinity_agent.permissions.manifest import PermissionLevel, DEFAULT_MANIFEST


class PermissionDenied(Exception):
    pass


class PermissionGateway:
    def __init__(
        self,
        manifest: dict[str, PermissionLevel] | None = None,
        confirm_callback=None,
        notify_callback=None,
    ):
        self._manifest = dict(DEFAULT_MANIFEST)
        if manifest:
            self._manifest.update(manifest)
        self._confirm_callback = confirm_callback or self._default_confirm
        self._notify_callback = notify_callback or self._default_notify

    def _default_confirm(self, tool_name: str, args: dict) -> bool:
        return False  # deny by default in headless contexts

    def _default_notify(self, tool_name: str, args: dict) -> None:
        pass

    def level(self, tool_name: str) -> PermissionLevel:
        return self._manifest.get(tool_name, PermissionLevel.CONFIRM)

    def check(self, tool_name: str, args: dict) -> None:
        """Raise PermissionDenied if the tool cannot proceed."""
        lvl = self.level(tool_name)
        if lvl == PermissionLevel.FORBIDDEN:
            raise PermissionDenied(f"Tool '{tool_name}' is forbidden")
        if lvl == PermissionLevel.CONFIRM:
            if not self._confirm_callback(tool_name, args):
                raise PermissionDenied(f"User denied '{tool_name}'")
        if lvl == PermissionLevel.NOTIFY:
            self._notify_callback(tool_name, args)
        # AUTO: pass through silently
