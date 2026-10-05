"""Native plugin registration; no core replacement or background work."""
from .heartbeat_tool import SCHEMA, HeartbeatTool


def register(ctx):
    from hermes_constants import get_hermes_home
    tool = HeartbeatTool(get_hermes_home().resolve())
    ctx.register_tool(
        name="session_heartbeat", toolset="session_heartbeat", schema=SCHEMA,
        handler=tool, description=SCHEMA["description"], emoji="♥",
    )
