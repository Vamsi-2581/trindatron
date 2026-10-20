from enum import Enum


class PermissionLevel(str, Enum):
    AUTO = "auto"         # execute silently
    NOTIFY = "notify"     # execute and log
    CONFIRM = "confirm"   # require explicit user approval
    FORBIDDEN = "forbidden"  # never execute


DEFAULT_MANIFEST: dict[str, PermissionLevel] = {
    # Filesystem
    "read_file": PermissionLevel.AUTO,
    "list_directory": PermissionLevel.AUTO,
    "write_file": PermissionLevel.CONFIRM,
    # Shell
    "execute_shell": PermissionLevel.CONFIRM,
    # Browser
    "browser_navigate": PermissionLevel.AUTO,
    "browser_get_text": PermissionLevel.AUTO,
    "browser_screenshot": PermissionLevel.AUTO,
    # Reading a page is passive; acting on it is not. A click can submit
    # a form and a fill can enter data on a third-party site, so both
    # pause for the user exactly like write_file/execute_shell do.
    "browser_click": PermissionLevel.CONFIRM,
    "browser_fill": PermissionLevel.CONFIRM,
    # Search
    "web_search": PermissionLevel.AUTO,
    # Desktop — ALL actions are CONFIRM; none are AUTO or NOTIFY.
    # The virtual display (Xvfb) is isolated from the Windows host, but the
    # agent can still capture sensitive content visible in the virtual environment.
    "desktop_screenshot":    PermissionLevel.CONFIRM,
    "desktop_click":         PermissionLevel.CONFIRM,
    "desktop_double_click":  PermissionLevel.CONFIRM,
    "desktop_right_click":   PermissionLevel.CONFIRM,
    "desktop_type":          PermissionLevel.CONFIRM,
    "desktop_key":           PermissionLevel.CONFIRM,
    "desktop_scroll":        PermissionLevel.CONFIRM,
    "desktop_drag":          PermissionLevel.CONFIRM,
    "desktop_move":          PermissionLevel.CONFIRM,
    "desktop_open":          PermissionLevel.CONFIRM,
    "desktop_get_windows":   PermissionLevel.CONFIRM,
    # Clipboard and credential tools — FORBIDDEN: not implemented, blocked by policy.
    # These require a separate security review before they can be activated.
    "desktop_clipboard_read":  PermissionLevel.FORBIDDEN,
    "desktop_clipboard_write": PermissionLevel.FORBIDDEN,
    "desktop_type_password":   PermissionLevel.FORBIDDEN,
    "desktop_form_submit":     PermissionLevel.FORBIDDEN,
}
