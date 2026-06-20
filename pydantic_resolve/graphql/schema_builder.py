"""
GraphQL Schema builder from ERD and @query decorated methods.

This module provides the SchemaBuilder class that builds GraphQL SDL strings.
The implementation delegates to SDLBuilder internally.
"""

import re
import warnings
from typing import Any
from pydantic_resolve.graphql.schema.generators.sdl_builder import SDLBuilder
from pydantic_resolve.utils.er_diagram import ErDiagram, Entity


class SchemaPatchResult:
    """Result of a schema patch operation.

    Attributes:
        merged_schema: The final merged schema string
        warnings: List of warning messages for conflicts
        added: List of added elements (types, fields, queries, mutations, inputs)
        modified: List of modified elements
        conflicts: List of conflict descriptions
    """

    def __init__(self):
        self.merged_schema: str = ""
        self.warnings: list[str] = []
        self.added: list[str] = []
        self.modified: list[str] = []
        self.conflicts: list[str] = []

    def __str__(self) -> str:
        return self.merged_schema


class SchemaBuilder:
    """
    Build GraphQL Schema from ERD and @query decorated methods.

    This class delegates to SDLBuilder internally.
    """

    def __init__(self, er_diagram: ErDiagram, validate_conflicts: bool = True, enable_pagination: bool = False):
        """
        Args:
            er_diagram: Entity relationship diagram
            validate_conflicts: Whether to validate field name conflicts (default True)
            enable_pagination: When True, one-to-many fields use Result types
        """
        self.er_diagram = er_diagram
        self.validate_conflicts = validate_conflicts
        self.enable_pagination = enable_pagination
        self._builder = SDLBuilder(er_diagram, validate_conflicts, enable_pagination=enable_pagination)

    def build_schema(self) -> str:
        """
        Build complete GraphQL Schema

        Returns:
            GraphQL Schema string
        """
        return self._builder.generate()

    def _extract_query_methods(self, entity: type) -> list[dict]:
        """Extract all @query decorated methods from an Entity."""
        return self._builder._extract_query_methods(entity)

    def _extract_mutation_methods(self, entity: type) -> list[dict]:
        """Extract all @mutation decorated methods from an Entity."""
        return self._builder._extract_mutation_methods(entity)

    def patch(self, schema: str | ErDiagram | dict[str, Any]) -> SchemaPatchResult:
        """Apply incremental schema updates and merge with existing schema.

        Conflict resolution rule: "新增优先于覆盖、覆盖优先于删除"
        (Add > Modify > Delete). When conflicts occur, warnings are issued
        but the patch version is retained.

        Args:
            schema: Patch schema, can be:
                - SDL string: GraphQL schema language string
                - ErDiagram: Entity diagram with additional types/queries/mutations
                - dict: Structure with keys 'types', 'queries', 'mutations', 'inputs'

        Returns:
            SchemaPatchResult containing merged schema and conflict info.

        Example:
            >>> builder = SchemaBuilder(er_diagram)
            >>> patch_sdl = '''
            ... type User {
            ...   newField: String
            ... }
            ... extend type Query {
            ...   newQuery: User
            ... }
            ... '''
            >>> result = builder.patch(patch_sdl)
            >>> print(result.merged_schema)
        """
        result = SchemaPatchResult()

        current_schema = self.build_schema()
        current_parsed = self._parse_sdl(current_schema)

        if isinstance(schema, str):
            patch_parsed = self._parse_sdl(schema)
        elif isinstance(schema, ErDiagram):
            patch_builder = SchemaBuilder(schema, validate_conflicts=False, enable_pagination=self.enable_pagination)
            patch_sdl = patch_builder.build_schema()
            patch_parsed = self._parse_sdl(patch_sdl)
        elif isinstance(schema, dict):
            patch_parsed = self._parse_dict_schema(schema)
        else:
            raise TypeError(f"Unsupported schema type: {type(schema)}. Use str, ErDiagram, or dict.")

        merged = self._merge_schemas(current_parsed, patch_parsed, result)
        result.merged_schema = self._build_sdl_from_parsed(merged)

        for warning in result.warnings:
            warnings.warn(warning, stacklevel=2)

        return result

    def _parse_sdl(self, sdl: str) -> dict[str, Any]:
        """Parse SDL string into structured format.

        Returns dict with keys: 'types', 'queries', 'mutations', 'inputs', 'enums'
        """
        parsed: dict[str, Any] = {
            'types': {},
            'queries': {},
            'mutations': {},
            'inputs': {},
            'enums': {},
        }

        type_pattern = r'(type|enum|input)\s+(\w+)\s*\{([^}]*)\}'
        extend_pattern = r'extend\s+(type|enum|input)\s+(\w+)\s*\{([^}]*)\}'

        for match in re.finditer(extend_pattern, sdl):
            type_kind, type_name, body = match.groups()
            fields = self._parse_fields(body)
            if type_kind == 'type':
                if type_name == 'Query':
                    parsed['queries'].update(fields)
                elif type_name == 'Mutation':
                    parsed['mutations'].update(fields)
                else:
                    if type_name not in parsed['types']:
                        parsed['types'][type_name] = {}
                    parsed['types'][type_name].update(fields)
            elif type_kind == 'input':
                if type_name not in parsed['inputs']:
                    parsed['inputs'][type_name] = {}
                parsed['inputs'][type_name].update(fields)
            elif type_kind == 'enum':
                if type_name not in parsed['enums']:
                    parsed['enums'][type_name] = []
                parsed['enums'][type_name].extend(fields.keys())

        for match in re.finditer(type_pattern, sdl):
            type_kind, type_name, body = match.groups()
            fields = self._parse_fields(body)
            if type_kind == 'type':
                if type_name == 'Query':
                    parsed['queries'].update(fields)
                elif type_name == 'Mutation':
                    parsed['mutations'].update(fields)
                else:
                    if type_name not in parsed['types']:
                        parsed['types'][type_name] = {}
                    parsed['types'][type_name].update(fields)
            elif type_kind == 'input':
                if type_name not in parsed['inputs']:
                    parsed['inputs'][type_name] = {}
                parsed['inputs'][type_name].update(fields)
            elif type_kind == 'enum':
                if type_name not in parsed['enums']:
                    parsed['enums'][type_name] = []
                parsed['enums'][type_name].extend(fields.keys())

        return parsed

    def _parse_fields(self, body: str) -> dict[str, str]:
        """Parse field definitions from type body.

        Returns dict mapping field name to full field definition string.
        """
        fields: dict[str, str] = {}
        lines = body.strip().split('\n')
        for line in lines:
            line = line.strip()
            if not line or line.startswith('"'):
                continue
            match = re.match(r'(\w+)(\([^)]*\))?\s*:\s*([^!]+!?)\s*', line)
            if match:
                field_name = match.group(1)
                fields[field_name] = line.strip()
        return fields

    def _parse_dict_schema(self, schema_dict: dict[str, Any]) -> dict[str, Any]:
        """Parse dict schema format into structured format."""
        parsed: dict[str, Any] = {
            'types': {},
            'queries': {},
            'mutations': {},
            'inputs': {},
            'enums': {},
        }

        for type_name, fields in schema_dict.get('types', {}).items():
            parsed['types'][type_name] = fields if isinstance(fields, dict) else self._parse_fields(str(fields))

        for query_name, definition in schema_dict.get('queries', {}).items():
            parsed['queries'][query_name] = definition if isinstance(definition, str) else f"{query_name}: {definition}"

        for mutation_name, definition in schema_dict.get('mutations', {}).items():
            parsed['mutations'][mutation_name] = definition if isinstance(definition, str) else f"{mutation_name}: {definition}"

        for input_name, fields in schema_dict.get('inputs', {}).items():
            parsed['inputs'][input_name] = fields if isinstance(fields, dict) else self._parse_fields(str(fields))

        return parsed

    def _merge_schemas(
        self,
        current: dict[str, Any],
        patch: dict[str, Any],
        result: SchemaPatchResult
    ) -> dict[str, Any]:
        """Merge two parsed schemas with conflict resolution.

        Conflict rule: Add > Modify > Delete. Patch takes precedence.
        """
        merged: dict[str, Any] = {
            'types': {},
            'queries': {},
            'mutations': {},
            'inputs': {},
            'enums': {},
        }

        for category in ['types', 'inputs', 'enums']:
            for name, fields in current[category].items():
                merged[category][name] = dict(fields) if isinstance(fields, dict) else list(fields)

            for name, fields in patch[category].items():
                if name not in merged[category]:
                    merged[category][name] = dict(fields) if isinstance(fields, dict) else list(fields)
                    result.added.append(f"{category[:-1]} {name}")
                else:
                    if isinstance(fields, dict):
                        for field_name, field_def in fields.items():
                            if field_name not in merged[category][name]:
                                merged[category][name][field_name] = field_def
                                result.added.append(f"{category[:-1]} {name}.{field_name}")
                            else:
                                existing = merged[category][name][field_name]
                                if existing != field_def:
                                    result.warnings.append(
                                        f"Conflict: {category[:-1]} {name}.{field_name} "
                                        f"modified. Existing: '{existing}', Patch: '{field_def}'. "
                                        f"Patch version retained (覆盖优先于删除)."
                                    )
                                    result.conflicts.append(f"{category[:-1]} {name}.{field_name}")
                                    result.modified.append(f"{category[:-1]} {name}.{field_name}")
                                merged[category][name][field_name] = field_def
                    else:
                        merged_set = set(merged[category][name])
                        for item in fields:
                            if item not in merged_set:
                                merged[category][name].append(item)
                                result.added.append(f"{category[:-1]} {name}.{item}")

        for category in ['queries', 'mutations']:
            for name, definition in current[category].items():
                merged[category][name] = definition

            for name, definition in patch[category].items():
                if name not in merged[category]:
                    merged[category][name] = definition
                    result.added.append(f"{category[:-1]} {name}")
                else:
                    existing = merged[category][name]
                    if existing != definition:
                        result.warnings.append(
                            f"Conflict: {category[:-1]} {name} modified. "
                            f"Existing: '{existing}', Patch: '{definition}'. "
                            f"Patch version retained (新增优先于覆盖)."
                        )
                        result.conflicts.append(f"{category[:-1]} {name}")
                        result.modified.append(f"{category[:-1]} {name}")
                    merged[category][name] = definition

        return merged

    def _build_sdl_from_parsed(self, parsed: dict[str, Any]) -> str:
        """Build SDL string from parsed schema structure."""
        parts: list[str] = []

        for enum_name, values in parsed['enums'].items():
            if values:
                values_str = "\n".join(f"  {v}" for v in values)
                parts.append(f"enum {enum_name} {{\n{values_str}\n}}")

        for input_name, fields in parsed['inputs'].items():
            if fields:
                fields_str = "\n".join(f"  {v}" for v in fields.values())
                parts.append(f"input {input_name} {{\n{fields_str}\n}}")

        for type_name, fields in parsed['types'].items():
            if fields:
                fields_str = "\n".join(f"  {v}" for v in fields.values())
                parts.append(f"type {type_name} {{\n{fields_str}\n}}")

        if parsed['queries']:
            queries_str = "\n".join(f"  {v}" for v in parsed['queries'].values())
            parts.append(f"type Query {{\n{queries_str}\n}}")

        if parsed['mutations']:
            mutations_str = "\n".join(f"  {v}" for v in parsed['mutations'].values())
            parts.append(f"type Mutation {{\n{mutations_str}\n}}")

        return "\n\n".join(parts) + "\n"
