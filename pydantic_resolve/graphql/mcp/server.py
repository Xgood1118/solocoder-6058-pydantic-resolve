"""MCP Server implementation for pydantic-resolve GraphQL.

This module provides the main entry point for creating an MCP server that exposes
pydantic-resolve GraphQL applications with progressive disclosure support.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from typing import TYPE_CHECKING

from pydantic_resolve.graphql.mcp.managers.multi_app_manager import MultiAppManager
from pydantic_resolve.graphql.mcp.tools.multi_app_tools import register_multi_app_tools
from pydantic_resolve.graphql.mcp.types.app_config import AppConfig, load_app_configs_from_yaml

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _setup_sighup_handler(manager: MultiAppManager) -> None:
    """Set up SIGHUP signal handler for config hot-reload.

    On Unix systems, SIGHUP triggers a config reload.
    On Windows, this is a no-op (file watcher is used instead).

    Args:
        manager: MultiAppManager instance to reload
    """
    if sys.platform == 'win32':
        logger.info("SIGHUP not supported on Windows. Use file watcher for hot-reload.")
        return

    def _handle_sighup(signum, frame):
        logger.info("Received SIGHUP, reloading configuration...")
        try:
            result = manager.reload_config()
            logger.info(
                f"Config reloaded. Added: {result['added']}, "
                f"Unchanged: {result['unchanged']}, Errors: {result['errors']}"
            )
        except Exception as e:
            logger.error(f"Config reload failed: {e}")

    signal.signal(signal.SIGHUP, _handle_sighup)
    logger.info("SIGHUP handler registered for config hot-reload")


def _setup_file_watcher(manager: MultiAppManager, poll_interval: int = 5) -> threading.Thread | None:
    """Set up file watcher for config hot-reload (Windows compatible).

    Starts a background thread that polls the config file for modifications.
    When the file changes, triggers a config reload.

    Args:
        manager: MultiAppManager instance to reload
        poll_interval: How often to check for file changes (seconds)

    Returns:
        Thread object if file watcher was started, None otherwise
    """
    if manager.config_path is None:
        return None

    last_mtime = 0
    stop_event = threading.Event()

    def _watch_loop():
        nonlocal last_mtime
        while not stop_event.is_set():
            try:
                current_mtime = os.path.getmtime(manager.config_path)
                if current_mtime > last_mtime:
                    if last_mtime > 0:
                        logger.info(
                            f"Config file changed (mtime: {current_mtime}), "
                            f"reloading configuration..."
                        )
                        try:
                            result = manager.reload_config()
                            logger.info(
                                f"Config reloaded via file watcher. "
                                f"Added: {result['added']}, "
                                f"Unchanged: {result['unchanged']}, "
                                f"Errors: {result['errors']}"
                            )
                        except Exception as e:
                            logger.error(f"Config reload via file watcher failed: {e}")
                    last_mtime = current_mtime
            except FileNotFoundError:
                logger.warning(f"Config file not found: {manager.config_path}")
            except Exception as e:
                logger.error(f"File watcher error: {e}")

            time.sleep(poll_interval)

    watcher_thread = threading.Thread(
        target=_watch_loop,
        name="config-watcher",
        daemon=True,
    )
    watcher_thread.start()
    logger.info(f"File watcher started for config: {manager.config_path}")

    return watcher_thread


def create_mcp_server(
    apps: list[AppConfig] | None = None,
    name: str = "Pydantic-Resolve GraphQL API",
    config_path: str | None = None,
    enable_hot_reload: bool = True,
    file_watcher_poll_interval: int = 5,
) -> "FastMCP":
    """Create an MCP server that exposes multiple ErDiagram as independent GraphQL apps.

    This function creates a FastMCP server with progressive disclosure support,
    allowing AI agents to discover and interact with GraphQL APIs incrementally.

    Progressive Disclosure Layers:
    - Layer 0: list_apps - Discover available applications
    - Layer 1: list_queries, list_mutations - List operations
    - Layer 2: get_query_schema, get_mutation_schema - Get detailed schema
    - Layer 3: graphql_query. graphql_mutation - Execute operations

    Hot-Reload Support:
    - When config_path is provided, apps are loaded from the YAML file
    - SIGHUP (Unix) or file modification (Windows) triggers config reload
    - Existing apps continue serving during reload (zero downtime)
    - New apps are registered automatically

    Args:
        apps: List of app configurations. If None and config_path is provided,
            apps will be loaded from the YAML file. Each config must include:
            - name: Application name (required)
            - er_diagram: ErDiagram instance (required)
            - description: Application description (optional)
            - query_description: Query type description (optional)
            - mutation_description: Mutation type description (optional)
        name: MCP server name (default: "Pydantic-Resolve GraphQL API")
        config_path: Optional path to YAML config file for app definitions
            and hot-reload support
        enable_hot_reload: Whether to enable config hot-reload via SIGHUP
            and file watcher (default: True)
        file_watcher_poll_interval: How often to check for config file changes
            in seconds (default: 5)

    Returns:
        A configured FastMCP server instance ready to run

    Example - Direct app configuration:
        ```python
        from pydantic_resolve import base_entity
        from pydantic_resolve.graphql.mcp import create_mcp_server

        BaseEntity = base_entity()

        apps = [
            {
                "name": "blog",
                "er_diagram": BaseEntity.get_diagram(),
                "description": "Blog system with users and posts",
            }
        ]

        mcp = create_mcp_server(apps=apps, name="Blog API")
        mcp.run()
        ```

    Example - YAML config with hot-reload:
        ```python
        from pydantic_resolve.graphql.mcp import create_mcp_server

        mcp = create_mcp_server(
            config_path="apps.yaml",
            name="Multi-App API",
            enable_hot_reload=True,
        )
        mcp.run()
        ```

    Raises:
        ValueError: If neither apps nor config_path is provided, or if
            both are provided but result in an empty app list
    """
    from fastmcp import FastMCP

    if apps is None and config_path is None:
        raise ValueError("Either apps or config_path must be provided")

    if config_path is not None:
        yaml_apps = load_app_configs_from_yaml(config_path)
        if apps is None:
            apps = yaml_apps
        else:
            apps = list(apps) + yaml_apps

    if not apps:
        raise ValueError("No apps found in configuration")

    # Create manager with all app resources
    manager = MultiAppManager(apps, config_path=config_path)

    # Set up hot-reload if enabled
    if enable_hot_reload and config_path is not None:
        _setup_sighup_handler(manager)
        _setup_file_watcher(manager, file_watcher_poll_interval)

    # Create FastMCP server
    mcp = FastMCP(name)

    # Store manager reference on server for external access
    mcp._multi_app_manager = manager

    # Register all tools
    register_multi_app_tools(mcp, manager)

    return mcp
