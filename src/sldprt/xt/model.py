"""Navigable B-rep model over decoded nodes."""

from __future__ import annotations

from dataclasses import dataclass, field

from .binary import Node

VOID = "V"

COLOUR_ATTRIBUTE = "SDL/TYSA_COLOUR"


@dataclass
class Model:
    nodes: list[Node]
    by_index: dict[int, Node] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.by_index:
            self.by_index = {n.index: n for n in self.nodes}

    def get(self, index: int) -> Node | None:
        return self.by_index.get(index) if index else None

    def of_type(self, name: str) -> list[Node]:
        return [n for n in self.nodes if n.name == name]


    def chain(self, start: int, link: str = "next") -> list[Node]:
        out: list[Node] = []
        seen: set[int] = set()
        node = self.get(start)
        while node is not None and node.index not in seen:
            seen.add(node.index)
            out.append(node)
            nxt = node.values.get(link)
            node = self.get(nxt) if isinstance(nxt, int) else None
        return out

    def ring(self, start: int) -> list[Node]:
        return self.chain(start, "forward")


    @property
    def bodies(self) -> list[Node]:
        return self.of_type("BODY")

    def shells_of(self, body: Node) -> list[Node]:
        return self.chain(body.values.get("shell", 0))

    def regions_of(self, body: Node) -> list[Node]:
        return self.chain(body.values.get("region", 0))

    def faces_of(self, shell: Node) -> list[Node]:
        return self.chain(shell.values.get("face", 0))

    def loops_of(self, face: Node) -> list[Node]:
        return self.chain(face.values.get("loop", 0))

    def halfedges_of(self, loop: Node) -> list[Node]:
        return self.ring(loop.values.get("halfedge", 0))

    def surface_of(self, face: Node) -> Node | None:
        return self.get(face.values.get("surface", 0))

    def curve_of(self, edge: Node) -> Node | None:
        return self.get(edge.values.get("curve", 0))

    def vertex_point(self, vertex: Node | None) -> list[float] | None:
        if vertex is None:
            return None
        point = self.get(vertex.values.get("point", 0))
        return list(point.values["pvec"]) if point else None

    def is_solid_region(self, region: Node) -> bool:
        kind = region.values.get("type")
        text = chr(kind) if isinstance(kind, int) else str(kind)
        return text != VOID

    def is_solid_body(self, body: Node) -> bool:
        return any(self.is_solid_region(r) for r in self.regions_of(body))


    @staticmethod
    def characters(values) -> str:
        if not isinstance(values, list):
            values = [values]
        return "".join(
            chr(c) if isinstance(c, int) else str(c) for c in values if c not in (0, "")
        )

    def _definition_name(self, definition: Node | None) -> str:
        if definition is None:
            return ""
        identifier = self.get(definition.values.get("identifier", 0))
        if identifier is None:
            return ""
        return self.characters(identifier.values.get("string", []))

    def attributes_named(self, prefix: str) -> list[Node]:
        wanted = {
            definition.index
            for definition in self.of_type("ATTRIB_DEF")
            if self._definition_name(definition).startswith(prefix)
        }
        return [
            node
            for node in self.of_type("ATTRIBUTE")
            if node.values.get("definition") in wanted
        ]

    def attribute_reals(self, attribute: Node) -> list[float]:
        out: list[float] = []
        fields = attribute.values.get("fields", [])
        for index in fields if isinstance(fields, list) else [fields]:
            node = self.get(index) if isinstance(index, int) else None
            if node is not None and node.name == "REAL_VALUES":
                values = node.values.get("values", [])
                out.extend(values if isinstance(values, list) else [values])
        return out

    def colours(self) -> dict[int, tuple[float, float, float]]:
        found: dict[int, tuple[float, float, float]] = {}
        for attribute in self.attributes_named(COLOUR_ATTRIBUTE):
            values = self.attribute_reals(attribute)
            if len(values) < 3:
                continue
            owner = attribute.values.get("owner")
            if isinstance(owner, int) and owner:
                found[owner] = (values[0], values[1], values[2])
        return found


def build(nodes: list[Node]) -> Model:
    return Model(nodes=nodes)

