"""Multi-app manager for MCP server.

This module provides the MultiAppManager class which manages multiple GraphQL applications,
each backed by an ErDiagram. It handles app registration, lookup, and routing.
"""

import logging
import threading
from typing import TYPE_CHECKING

from pydantic_resolve.graphql.handler import GraphQLHandler
from pydantic_resolve.graphql.schema.generators.introspection_generator import IntrospectionGenerator
from pydantic_resolve.graphql.schema.generators.sdl_builder import SDLBuilder
from pydantic_resolve.graphql.mcp.builders.introspection_query_helper import IntrospectionQueryHelper
from pydantic_resolve.graphql.mcp.managers.app_resources import AppResources
from pydantic_resolve.graphql.mcp.types.app_config import AppConfig, load_app_configs_from_yaml

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class MultiAppManager:
    """Manages multiple GraphQL applications for MCP server.

    This manager handles:
    - Registration of multiple apps from AppConfig list
    - App lookup by name (with smart routing)
    - Resource creation for each app (Handler, IntrospectionQueryHelper, SDLBuilder)
    - Hot-reload of app configurations from YAML file
    - Zero-downtime reload: existing apps continue serving during reload

    Each app is independent and backed by its own ErDiagram.
    """

    def __init__(self, apps: list[AppConfig], config_path: str | None = None):
        """Initialize the manager with app configurations.

        Args:
            apps: List of AppConfig dictionaries, each containing:
                - name: Application name (required)
                - er_diagram: ErDiagram instance (required)
                - description: Application description (optional)
                - query_description: Query type description (optional)
                - mutation_description: Mutation type description (optional)
            config_path: Optional path to YAML config file for hot-reload

        Raises:
            ValueError: If an app with the same name already exists
        """
        self.apps: dict[str, AppResources] = {}
        self._app_names_lower: dict[str, str] = {}  # lowercase -> original case
        self._config_path = config_path
        self._lock = threading.RLock()
        self._reload_count = 0

        for app_config in apps:
            resources = self._create_app_resources(app_config)
            self._register_app(resources)

    def _create_app_resources(self, config: AppConfig) -> AppResources:
        """Create AppResources from AppConfig.

        This method:
        1. Creates a GraphQLHandler from the ErDiagram
        2. Creates an IntrospectionQueryHelper from introspection data
        3. Creates an SDLBuilder for schema generation

        Args:
            config: Application configuration

        Returns:
            AppResources instance with all components initialized
        """
        er_diagram = config.er_diagram
        name = config.name
        description = config.description or ""
        enable_from_attribute = config.enable_from_attribute_in_type_adapter

        # Create GraphQLHandler
        handler = GraphQLHandler(
            er_diagram=er_diagram,
            enable_from_attribute_in_type_adapter=enable_from_attribute,
        )

        # Create IntrospectionQueryHelper using IntrospectionGenerator
        introspection_generator = IntrospectionGenerator(
            er_diagram=er_diagram,
            query_map=handler.query_map,
            mutation_map=handler.mutation_map
        )
        introspection_data = introspection_generator.generate()
        entity_names = {cfg.kls.__name__ for cfg in er_diagram.entities}
        introspection_helper = IntrospectionQueryHelper(introspection_data, entity_names)

        # Create SDLBuilder
        sdl_builder = SDLBuilder(
            er_diagram=er_diagram,
        )

        return AppResources(
            name=name,
            description=description,
            handler=handler,
            introspection_helper=introspection_helper,
            sdl_builder=sdl_builder,
            context_extractor=config.context_extractor,
        )

    def _register_app(self, resources: AppResources) -> None:
        """Register an app's resources.

        Args:
            resources: AppResources to register

        Raises:
            ValueError: If an app with the same name already exists
        """
        name = resources.name
        name_lower = name.lower()

        if name_lower in self._app_names_lower:
            raise ValueError(f"App with name '{name}' already exists")

        self.apps[name] = resources
        self._app_names_lower[name_lower] = name

    def get_app(self, name: str) -> AppResources:
        """Get app resources by name.

        Supports smart routing:
        - Exact match: "MyApp" -> "MyApp"
        - Case-insensitive match: "myapp" -> "MyApp"

        Args:
            name: Application name

        Returns:
            AppResources for the matching app

        Raises:
            ValueError: If app not found
        """
        # Try exact match first
        if name in self.apps:
            return self.apps[name]

        # Try case-insensitive match
        name_lower = name.lower()
        if name_lower in self._app_names_lower:
            return self.apps[self._app_names_lower[name_lower]]

        raise ValueError(f"App '{name}' not found. Available apps: {list(self.apps.keys())}")

    def list_apps(self) -> list[str]:
        """Get list of all registered app names.

        Returns:
            List of app names
        """
        return list(self.apps.keys())

    def get_app_info(self, name: str) -> dict:
        """Get detailed information about an app.

        Args:
            name: Application name

        Returns:
            Dictionary with app information:
                - name: App name
                - description: App description
                - query_count: Number of queries
                - mutation_count: Number of mutations
        """
        app = self.get_app(name)
        return {
            "name": app.name,
            "description": app.description,
            "query_count": len(app.query_names),
            "mutation_count": len(app.mutation_names),
        }

    def add_app(self, config: AppConfig) -> AppResources:
        """Add a new app dynamically without interrupting existing apps.

        Args:
            config: AppConfig for the new app

        Returns:
            The created AppResources

        Raises:
            ValueError: If an app with the same name already exists
        """
        with self._lock:
            name_lower = config.name.lower()
            if name_lower in self._app_names_lower:
                raise ValueError(f"App with name '{config.name}' already exists")

            resources = self._create_app_resources(config)
            self._register_app(resources)
            logger.info(f"Added new app: {config.name}")
            return resources

    def _unregister_app(self, name: str) -> None:
        """Unregister an app (internal use).

        Note: Existing requests in-flight will continue using the old resources.

        Args:
            name: Application name to unregister
        """
        with self._lock:
            name_lower = name.lower()
            if name_lower in self._app_names_lower:
                original_name = self._app_names_lower[name_lower]
                del self.apps[original_name]
                del self._app_names_lower[name_lower]
                logger.info(f"Unregistered app: {original_name}")

    def reload_config(self) -> dict:
        """Reload app configuration from YAML file.

        This method:
        1. Loads new configuration from YAML file
        2. Registers any new apps found in the config
        3. Existing apps continue serving requests during reload (zero downtime)
        4. Apps no longer in config are NOT removed (to avoid interrupting requests)

        Returns:
            Dictionary with reload statistics:
                - reload_count: Total number of reloads
                - added: List of newly added app names
                - unchanged: List of unchanged app names
                - errors: List of error messages

        Raises:
            ValueError: If no config_path was set during initialization
        """
        if self._config_path is None:
            raise ValueError(
                "config_path not set. Initialize MultiAppManager with config_path "
                "or use load_app_configs_from_yaml() directly."
            )

        result = {
            "reload_count": self._reload_count + 1,
            "added": [],
            "unchanged": [],
            "errors": [],
        }

        try:
            new_configs = load_app_configs_from_yaml(self._config_path)
        except Exception as e:
            result["errors"].append(f"Failed to load config: {str(e)}")
            logger.error(f"Config reload failed: {e}")
            return result

        existing_names_lower = set(self._app_names_lower.keys())

        with self._lock:
            for config in new_configs:
                name_lower = config.name.lower()
                if name_lower in existing_names_lower:
                    result["unchanged"].append(config.name)
                    continue

                try:
                    self.add_app(config)
                    result["added"].append(config.name)
                except Exception as e:
                    result["errors"].append(f"Failed to add app '{config.name}': {str(e)}")
                    logger.error(f"Failed to add app '{config.name}': {e}")

            self._reload_count += 1

        logger.info(
            f"Config reload complete. Added: {result['added']}, "
            f"Unchanged: {result['unchanged']}, Errors: {result['errors']}"
        )

        return result

    @property
    def reload_count(self) -> int:
        """Get the number of times config has been reloaded."""
        return self._reload_count

    @property
    def config_path(self) -> str | None:
        """Get the YAML config file path."""
        return self._config_path
