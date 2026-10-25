"""Reshape and recolour a decoded node graph for writing."""

from __future__ import annotations

from .binary import Node
from .schema import DOUBLES_PER_TYPE, FieldSpec, Schema
from .text import NULL_DOUBLE

PARTITION_ONLY = {"WORLD"}


class WriteError(Exception):
    pass


def pointers(node: Node, layouts: dict[int, list[FieldSpec]]):
    return [spec for spec in layouts.get(node.node_type, []) if spec.ptr_class]


def rebuild(node: Node, values: dict) -> Node:
    return Node(
        node_type=node.node_type,
        name=node.name,
        index=node.index,
        values=values,
        start=node.start,
        end=node.end,
    )


def remap(
    nodes: list[Node],
    layouts: dict[int, list[FieldSpec]],
    change,
    *,
    skip=lambda node, spec: False,
) -> list[Node]:
    out = []
    for node in nodes:
        values = dict(node.values)
        for spec in pointers(node, layouts):
            if skip(node, spec):
                continue
            value = values.get(spec.name)
            if isinstance(value, list):
                values[spec.name] = [
                    change(v, node, spec) if isinstance(v, int) else v for v in value
                ]
            elif isinstance(value, int):
                values[spec.name] = change(value, node, spec)
        out.append(rebuild(node, values))
    return out


def drop_nodes(
    nodes: list[Node],
    names: set[str],
    layouts: dict[int, list[FieldSpec]],
    *,
    indices: set[int] | None = None,
) -> list[Node]:
    dropped = {n.index for n in nodes if n.name in names} | (indices or set())
    if not dropped:
        return nodes

    survivors = [
        node
        for node in nodes
        if node.name not in names and node.index not in dropped
    ]
    return remap(
        survivors, layouts, lambda index, *_: 0 if index in dropped else index
    )


def drop_partition_nodes(
    nodes: list[Node], layouts: dict[int, list[FieldSpec]]
) -> list[Node]:
    world = {node.index for node in nodes if node.name in PARTITION_ONLY}
    return drop_nodes(
        nodes,
        PARTITION_ONLY,
        layouts,
        indices={
            node.index
            for node in nodes
            if node.name == "TRANSFORM" and node.values.get("owner") in world
        },
    )


ASSEMBLY_TYPE = 10
INSTANCE_TYPE = 11
TRANSFORM_TYPE = 100

PART_TYPE_SOLID = 1


def _blank(spec: FieldSpec):
    count = (spec.n_elts or 1) * DOUBLES_PER_TYPE.get(spec.type, 1)
    if spec.type in DOUBLES_PER_TYPE:
        return [0.0] * count if count > 1 or spec.type != "f" else 0.0
    return [0] * count if count > 1 else 0


def _synthesise(node_type: int, index: int, fields: list[FieldSpec], **values) -> Node:
    filled = {spec.name: _blank(spec) for spec in fields}
    filled.update(values)
    return Node(
        node_type=node_type,
        name={
            ASSEMBLY_TYPE: "ASSEMBLY",
            INSTANCE_TYPE: "INSTANCE",
            TRANSFORM_TYPE: "TRANSFORM",
        }[node_type],
        index=index,
        values=filled,
    )


def wrap_in_assembly(
    nodes: list[Node],
    layouts: dict[int, list[FieldSpec]],
    current: Schema | None,
    *,
    least: int = 1,
) -> list[Node]:
    bodies = [n for n in nodes if n.name == "BODY"]
    if len(bodies) < least:
        return nodes

    for node_type in (ASSEMBLY_TYPE, INSTANCE_TYPE, TRANSFORM_TYPE):
        if node_type in layouts:
            continue
        if current is None:
            raise WriteError(
                f"{len(bodies)} bodies to root on an ASSEMBLY, but the "
                f"file's version schema is missing: type {node_type} is not "
                "in the partition. Add sch_<version>.s_t (see README)."
            )
        spec = next(
            (n for n in current.nodes.values() if n.node_type == node_type), None
        )
        if spec is None:
            raise WriteError(
                f"schema {current.key} declares no node type {node_type}"
            )
        layouts[node_type] = list(spec.effective_fields)

    free = max(n.index for n in nodes) + 1
    assembly_index = free
    instances = {body.index: free + 1 + 2 * i for i, body in enumerate(bodies)}
    transforms = {body.index: free + 2 + 2 * i for i, body in enumerate(bodies)}

    count = len(bodies)
    ids = {body.index: 2 * (count - i) - 1 for i, body in enumerate(bodies)}
    positions = {body.index: rank for rank, body in enumerate(bodies)}

    assembly = _synthesise(
        ASSEMBLY_TYPE,
        assembly_index,
        layouts[ASSEMBLY_TYPE],
        highest_node_id=2 * count + 1,
        res_size=bodies[0].values.get("res_size", 1000.0),
        res_linear=bodies[0].values.get("res_linear", 1e-8),
        state=1,
        type=PART_TYPE_SOLID,
        sub_instance=instances[bodies[0].index],
    )

    out: list[Node] = [assembly]
    for node in nodes:
        if node.name != "BODY":
            out.append(node)
            continue
        at = positions[node.index]
        out.append(
            _synthesise(
                INSTANCE_TYPE,
                instances[node.index],
                layouts[INSTANCE_TYPE],
                node_id=ids[node.index],
                type=PART_TYPE_SOLID,
                part=node.index,
                transform=transforms[node.index],
                assembly=assembly_index,
                next_in_part=(
                    instances[bodies[at + 1].index] if at + 1 < count else 0
                ),
                prev_in_part=instances[bodies[at - 1].index] if at else 0,
            )
        )
        values = dict(node.values)
        values["ref_instance"] = instances[node.index]
        out.append(
            Node(
                node_type=node.node_type,
                name=node.name,
                index=node.index,
                values=values,
                start=node.start,
                end=node.end,
            )
        )
        out.append(
            _synthesise(
                TRANSFORM_TYPE,
                transforms[node.index],
                layouts[TRANSFORM_TYPE],
                node_id=ids[node.index] + 1,
                owner=instances[node.index],
                rotation_matrix=[1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0],
                scale=1.0,
                perspective_vector=[NULL_DOUBLE] * 3,
            )
        )
    return out


def renumber(nodes: list[Node], layouts: dict[int, list[FieldSpec]]) -> list[Node]:
    mapping = {node.index: i for i, node in enumerate(nodes, start=1)}
    outside = len(nodes)

    def target(index: int) -> int:
        nonlocal outside
        if index == 0:
            return 0
        if index not in mapping:
            outside += 1
            mapping[index] = outside
        return mapping[index]

    return [
        Node(
            node_type=node.node_type,
            name=node.name,
            index=mapping[node.index],
            values=followed.values,
            start=node.start,
            end=node.end,
        )
        for node, followed in zip(
            nodes, remap(nodes, layouts, lambda index, *_: target(index)), strict=False
        )
    ]


_ATTRIBUTE_CHAINS = (("next", "previous"), ("next_of_type", "previous_of_type"))


def drop_attributes(
    nodes: list[Node], layouts: dict[int, list[FieldSpec]], names: set[str]
) -> list[Node]:
    from .model import Model

    model = Model(nodes=nodes)
    wanted = {
        definition.index
        for definition in model.of_type("ATTRIB_DEF")
        if model._definition_name(definition) in names
    }
    if not wanted:
        return nodes
    return drop_attribute_nodes(
        nodes,
        layouts,
        {
            node.index
            for node in nodes
            if node.name == "ATTRIBUTE" and node.values.get("definition") in wanted
        },
    )


def drop_attribute_nodes(
    nodes: list[Node], layouts: dict[int, list[FieldSpec]], doomed: set[int]
) -> list[Node]:
    by_index = {node.index: node for node in nodes}
    doomed = {i for i in doomed if i in by_index and by_index[i].name == "ATTRIBUTE"}
    if not doomed:
        return nodes

    spoken_for: set[int] = set()
    for node in nodes:
        if node.name != "ATTRIBUTE" or node.index in doomed:
            continue
        fields = node.values.get("fields")
        for target in fields if isinstance(fields, list) else [fields]:
            if isinstance(target, int) and target:
                spoken_for.add(target)
    values: set[int] = set()
    for index in doomed:
        fields = by_index[index].values.get("fields")
        for target in fields if isinstance(fields, list) else [fields]:
            if isinstance(target, int) and target and target not in spoken_for:
                values.add(target)

    def survivor(index: int, direction: str) -> int:
        seen = set()
        while index in doomed and index not in seen:
            seen.add(index)
            index = by_index[index].values.get(direction) or 0
        return index if index not in doomed else 0

    links = {name for pair in _ATTRIBUTE_CHAINS for name in pair}

    def heal(index: int, node: Node, spec: FieldSpec) -> int:
        if index in values:
            return 0
        if index not in doomed:
            return index
        return survivor(index, spec.name if spec.name in links else "next")

    healed = remap(
        [n for n in nodes if n.index not in doomed and n.index not in values],
        layouts,
        heal,
        skip=lambda node, spec: node.name == "POINTER_LIS_BLOCK"
        and spec.name == "entries",
    )
    return _compact_lists(healed, doomed)


def _compact_lists(nodes: list[Node], removed: set[int]) -> list[Node]:
    shrunk: dict[int, int] = {}
    out = []
    for node in nodes:
        if node.name != "POINTER_LIS_BLOCK":
            out.append(node)
            continue
        entries = node.values.get("entries")
        if not isinstance(entries, list):
            out.append(node)
            continue
        kept = [e for e in entries if not (isinstance(e, int) and e in removed)]
        gone = len(entries) - len(kept)
        if not gone:
            out.append(node)
            continue
        values = dict(node.values)
        values["entries"] = kept + [0] * gone
        values["n_entries"] = max(0, node.values.get("n_entries", 0) - gone)
        shrunk[node.index] = gone
        out.append(
            Node(
                node_type=node.node_type,
                name=node.name,
                index=node.index,
                values=values,
                start=node.start,
                end=node.end,
            )
        )

    if not shrunk:
        return out

    final = []
    for node in out:
        if node.name != "LIST":
            final.append(node)
            continue
        gone = 0
        block = node.values.get("list_block")
        seen = set()
        while isinstance(block, int) and block and block not in seen:
            seen.add(block)
            gone += shrunk.get(block, 0)
            block = next(
                (n.values.get("next_block") for n in out if n.index == block), 0
            )
        if not gone:
            final.append(node)
            continue
        values = dict(node.values)
        values["list_length"] = max(0, node.values.get("list_length", 0) - gone)
        final.append(
            Node(
                node_type=node.node_type,
                name=node.name,
                index=node.index,
                values=values,
                start=node.start,
                end=node.end,
            )
        )
    return final


def _repack_chained_lists(nodes: list[Node]) -> list[Node]:
    by_index = {n.index: n for n in nodes}
    fixed: dict[int, Node] = {}
    doomed: set[int] = set()
    for node in nodes:
        if node.name != "LIST":
            continue
        first = node.values.get("list_block")
        if not (isinstance(first, int) and first in by_index):
            continue
        chain = []
        block = first
        seen: set[int] = set()
        while (
            isinstance(block, int)
            and block in by_index
            and block not in seen
            and by_index[block].name == "POINTER_LIS_BLOCK"
        ):
            seen.add(block)
            chain.append(by_index[block])
            block = by_index[block].values.get("next_block")
        if len(chain) < 2:
            continue
        entries: list = []
        for b in chain:
            used = b.values.get("n_entries") or 0
            entries.extend((b.values.get("entries") or [])[:used])
        width = len(chain[0].values.get("entries") or []) or 1
        needed = max(1, -(-len(entries) // width))
        keep, surplus = chain[:needed], chain[needed:]
        at = 0
        for position, b in enumerate(keep):
            take = entries[at : at + width]
            at += len(take)
            values = dict(b.values)
            values["entries"] = list(take) + [0] * (width - len(take))
            values["n_entries"] = len(take)
            if position == len(keep) - 1:
                values["next_block"] = 0
            fixed[b.index] = rebuild(b, values)
        for b in surplus:
            doomed.add(b.index)
        values = dict(node.values)
        values["list_length"] = len(entries)
        if entries:
            values["finger_index"] = len(entries)
            holder = min((len(entries) - 1) // width, len(keep) - 1)
            values["finger_block"] = keep[holder].index
        fixed[node.index] = rebuild(node, values)
    if not fixed and not doomed:
        return nodes
    return [
        fixed.get(n.index, n) for n in nodes if n.index not in doomed
    ]


SOLIDWORKS_BOOKKEEPING: frozenset[str] = frozenset({
    "ATOM_FACE_ID_2001",
    "ATOM_ID_2001",
    "BODY_IN_LIGHTWEIGHT_PERM",
    "BODY_MATCH",
    "BODY_RECIPE_2001",
    "BODY_SCALING_FACTOR",
    "DOWNSTREAM_FACE_ID",
    "EDGE_PERM_ID_2001",
    "EDGE_PERM_ID_2_2003",
    "ENT_TIME_STAMP_2001",
    "FACE_ID_2001",
    "FACE_ID_2_2001",
    "FILLET_MUTUAL_FACE",
    "LAST_BODY_MODIFYING_FEATURE_ID",
    "SDL/TYSA_TRANSLUCENCY",
    "SDL/TYSA_TRANSLUCENCY_2",
    "SW_INTERNAL_BODY_NAME",
    "BODY_AUTOMATION_2_2001",
    "EDGE_AUTOMATION_2_2001",
    "FACE_AUTOMATION_2_2001",
    "VERTEX_AUTOMATION_2_2001",
})


def prepare(
    nodes: list[Node],
    layouts: dict[int, list[FieldSpec]],
    current: Schema | None = None,
) -> list[Node]:
    """Turn partition nodes into the nodes of a part transmit."""
    kept = drop_partition_nodes(nodes, layouts)
    kept = drop_attributes(kept, layouts, set(SOLIDWORKS_BOOKKEEPING))
    kept = spread_colours_to_faces(kept, layouts)
    kept = drop_attributes(kept, layouts, {PRIVATE_COLOUR_ATTRIBUTE})
    least = 1 if current is not None else 2
    kept = wrap_in_assembly(kept, layouts, current, least=least)
    root = next((n.index for n in kept if n.name == "ASSEMBLY"), None)
    if root is not None:
        kept = mark_as_multi_body(kept, root)
    return _repack_chained_lists(kept)


COLOUR_ATTRIBUTE = "SDL/TYSA_COLOUR"
COLOUR_TYPE_ID = 8001
COLOUR_LEGAL_OWNERS = (0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

PRIVATE_COLOUR_ATTRIBUTE = "SDL/TYSA_COLOUR_2"


class _Chains:
    def __init__(self, nodes: list[Node]) -> None:
        self.by_index = {node.index: node for node in nodes}
        self.changed: dict[int, dict] = {}
        self.born: list[Node] = []
        self._next = max((node.index for node in nodes), default=0) + 1

    def free(self) -> int:
        self._next += 1
        return self._next - 1

    def add(self, node: Node) -> None:
        self.by_index[node.index] = node
        self.born.append(node)

    def value(self, index: int, field: str):
        if index in self.changed and field in self.changed[index]:
            return self.changed[index][field]
        node = self.by_index.get(index)
        return node.values.get(field) if node else None

    def set(self, index: int, field: str, value) -> None:
        self.changed.setdefault(index, {})[field] = value

    def tail(self, head, field: str = "next") -> int:
        seen: set[int] = set()
        while isinstance(head, int) and head in self.by_index and head not in seen:
            seen.add(head)
            following = self.value(head, field)
            if not isinstance(following, int) or following not in self.by_index:
                return head
            head = following
        return 0

    def applied(self, nodes: list[Node]) -> list[Node]:
        def avec_changements(node: Node) -> Node:
            change = self.changed.get(node.index)
            if not change:
                return node
            values = dict(node.values)
            values.update(change)
            return rebuild(node, values)

        return [avec_changements(n) for n in nodes] + [
            avec_changements(n) for n in self.born
        ]


def _existing_chain(chains: _Chains, body: Node, definition: int) -> int:
    listing = chains.value(body.index, "attribute_chains")
    if not isinstance(listing, int) or listing not in chains.by_index:
        return 0
    block = chains.value(listing, "list_block")
    while isinstance(block, int) and block in chains.by_index:
        entries = list(chains.value(block, "entries") or [])
        used = chains.value(block, "n_entries") or 0
        for head in entries[:used]:
            if (
                isinstance(head, int)
                and head in chains.by_index
                and chains.value(head, "definition") == definition
            ):
                return head
        block = chains.value(block, "next_block")
    return 0


def _note_in_list(chains: _Chains, body: Node, head: int) -> None:
    listing = chains.value(body.index, "attribute_chains")
    if not isinstance(listing, int) or listing not in chains.by_index:
        raise WriteError(f"body {body.index} keeps no list of its attributes")

    block = chains.value(listing, "list_block")
    last = 0
    while isinstance(block, int) and block in chains.by_index:
        entries = list(chains.value(block, "entries") or [])
        used = chains.value(block, "n_entries") or 0
        if used < len(entries):
            entries[used] = head
            chains.set(block, "entries", entries)
            chains.set(block, "n_entries", used + 1)
            chains.set(
                listing, "list_length", (chains.value(listing, "list_length") or 0) + 1
            )
            return
        last = block
        block = chains.value(block, "next_block")

    if not last:
        raise WriteError(f"body {body.index} keeps no block of attribute kinds")
    template = chains.by_index[last]
    width = len(template.values.get("entries") or [])
    fresh = chains.free()
    chains.add(
        Node(
            node_type=template.node_type,
            name=template.name,
            index=fresh,
            values={
                "n_entries": 1,
                "index_map_offset": 0,
                "next_block": 0,
                "entries": [head] + [0] * (width - 1),
            },
        )
    )
    chains.set(last, "next_block", fresh)
    chains.set(listing, "list_length", (chains.value(listing, "list_length") or 0) + 1)


MULTI_BODY_ATTRIBUTE = "MULTI_BODY_ID_2001"
MULTI_BODY_TYPE_ID = 9000
MULTI_BODY_ACTIONS = (0, 0, 0, 0, 3, 5, 0, 0)
MULTI_BODY_FIELDS = (9, 1)
MULTI_BODY_LEGAL_OWNERS = (1,) + (0,) * 16


def mark_as_multi_body(nodes: list[Node], root: int) -> list[Node]:
    by_index = {n.index: n for n in nodes}
    if root not in by_index:
        return nodes
    template = next((n for n in nodes if n.name == "ATTRIB_DEF"), None)
    kind = next((n for n in nodes if n.name == "ATTRIBUTE"), None)
    listing_model = next((n for n in nodes if n.name == "LIST"), None)
    block_model = next((n for n in nodes if n.name == "POINTER_LIS_BLOCK"), None)
    if not (template and kind and listing_model and block_model):
        return nodes
    spelling = by_index.get(template.values.get("identifier"))
    if spelling is None:
        return nodes

    chains = _Chains(nodes)
    name_index, def_index = chains.free(), chains.free()
    attribute_index = chains.free()
    listing_index, block_index = chains.free(), chains.free()

    chains.add(
        Node(
            node_type=spelling.node_type,
            name=spelling.name,
            index=name_index,
            values={"string": [ord(c) for c in MULTI_BODY_ATTRIBUTE]},
        )
    )
    values = dict(template.values)
    values.update(
        identifier=name_index,
        type_id=MULTI_BODY_TYPE_ID,
        actions=_fitted(MULTI_BODY_ACTIONS, template, "actions"),
        fields=_fitted(MULTI_BODY_FIELDS, template, "fields"),
        legal_owners=_fitted(MULTI_BODY_LEGAL_OWNERS, template),
        next=0,
    )
    chains.add(
        Node(
            node_type=template.node_type,
            name=template.name,
            index=def_index,
            values=values,
        )
    )
    last = chains.tail(template.index)
    if last:
        chains.set(last, "next", def_index)

    node_id = (chains.value(root, "highest_node_id") or 0) + 1
    chains.set(root, "highest_node_id", node_id)
    valeurs = dict(kind.values)
    valeurs.update(
        node_id=node_id,
        definition=def_index,
        owner=root,
        next=0,
        previous=0,
        next_of_type=0,
        previous_of_type=0,
        fields=[0, 0],
    )
    chains.add(
        Node(
            node_type=kind.node_type,
            name=kind.name,
            index=attribute_index,
            values=valeurs,
        )
    )
    chains.set(root, "attributes_features", attribute_index)

    width = len(block_model.values.get("entries") or []) or 1
    valeurs = dict(block_model.values)
    valeurs.update(
        entries=[attribute_index] + [0] * (width - 1),
        n_entries=1,
        next_block=0,
    )
    chains.add(
        Node(
            node_type=block_model.node_type,
            name=block_model.name,
            index=block_index,
            values=valeurs,
        )
    )
    valeurs = dict(listing_model.values)
    valeurs.update(
        list_block=block_index,
        list_length=1,
        finger_index=1,
        finger_block=block_index,
        next=0,
    )
    chains.add(
        Node(
            node_type=listing_model.node_type,
            name=listing_model.name,
            index=listing_index,
            values=valeurs,
        )
    )
    chains.set(root, "attribute_chains", listing_index)
    return chains.applied(nodes)


def _fitted(mask: tuple[int, ...], template: Node, field: str = "legal_owners") -> list[int]:
    porte = template.values.get(field)
    if not isinstance(porte, (list, tuple)):
        return list(mask)
    width = len(porte)
    if not width or width >= len(mask):
        return list(mask) + [0] * (max(width, len(mask)) - len(mask))
    if any(mask[width:]):
        raise WriteError(
            f"{field}: {len(mask)} slots are needed and this schema declares "
            f"{width} — a bit that matters would be lost"
        )
    return list(mask[:width])


def spread_colours_to_faces(
    nodes: list[Node], layouts: dict[int, list[FieldSpec]]
) -> list[Node]:
    from .model import Model

    model = Model(nodes=nodes)
    chains = _Chains(nodes)
    by_index = chains.by_index
    names = {d.index: model._definition_name(d) for d in model.of_type("ATTRIB_DEF")}
    standard = next((i for i, n in names.items() if n == COLOUR_ATTRIBUTE), None)
    private = next(
        (i for i, n in names.items() if n == PRIVATE_COLOUR_ATTRIBUTE), None
    )

    stated: dict[int, list[float]] = {}
    for attribute in model.of_type("ATTRIBUTE"):
        if names.get(attribute.values.get("definition")) not in (
            COLOUR_ATTRIBUTE,
            PRIVATE_COLOUR_ATTRIBUTE,
        ):
            continue
        values = model.attribute_reals(attribute)
        owner = attribute.values.get("owner")
        if len(values) >= 3 and isinstance(owner, int) and owner:
            stated.setdefault(owner, values[:3])

    settled = {
        attribute.values.get("owner")
        for attribute in model.of_type("ATTRIBUTE")
        if names.get(attribute.values.get("definition")) == COLOUR_ATTRIBUTE
    }

    work: list[tuple[Node, list[tuple[Node, list[float]]]]] = []
    for body in model.bodies:
        colour = stated.get(body.index)
        faces = []
        for shell in model.shells_of(body):
            for face in model.faces_of(shell):
                paint = stated.get(face.index, colour)
                if paint is not None and face.index not in settled:
                    faces.append((face, paint))
        if faces:
            work.append((body, faces))
    if not work:
        return nodes

    template = by_index.get(standard if standard is not None else private)
    if template is None:
        raise WriteError("the part states no colour to spread")
    reals = next((n for n in nodes if n.name == "REAL_VALUES"), None)
    kind = next((n for n in nodes if n.name == "ATTRIBUTE"), None)
    if reals is None or kind is None:
        raise WriteError("the part carries no attribute to model a colour on")

    if standard is None:
        identifier = by_index.get(template.values.get("identifier"))
        if identifier is None:
            raise WriteError("the colour definition names no identifier")
        standard, name_index = chains.free(), chains.free()
        chains.add(
            Node(
                node_type=identifier.node_type,
                name=identifier.name,
                index=name_index,
                values={"string": [ord(c) for c in COLOUR_ATTRIBUTE]},
            )
        )
        values = dict(template.values)
        values.update(
            identifier=name_index,
            type_id=COLOUR_TYPE_ID,
            legal_owners=_fitted(COLOUR_LEGAL_OWNERS, template),
            next=0,
        )
        chains.add(
            Node(
                node_type=template.node_type,
                name=template.name,
                index=standard,
                values=values,
            )
        )
        last = chains.tail(min(names) if names else 0)
        if last:
            chains.set(last, "next", standard)

    for body, faces in work:
        head = _existing_chain(chains, body, standard)
        first = head
        earlier = chains.tail(head, "next_of_type") if head else 0
        for face, paint in faces:
            value_index, attribute_index = chains.free(), chains.free()
            chains.add(
                Node(
                    node_type=reals.node_type,
                    name=reals.name,
                    index=value_index,
                    values={"values": list(paint)},
                )
            )

            node_id = (chains.value(body.index, "highest_node_id") or 0) + 1
            chains.set(body.index, "highest_node_id", node_id)

            tail = chains.tail(chains.value(face.index, "attributes_features"))
            attribute = Node(
                node_type=kind.node_type,
                name=kind.name,
                index=attribute_index,
                values={
                    "node_id": node_id,
                    "definition": standard,
                    "owner": face.index,
                    "next": 0,
                    "previous": tail,
                    "next_of_type": 0,
                    "previous_of_type": earlier,
                    "fields": value_index,
                },
            )
            chains.add(attribute)

            if tail:
                chains.set(tail, "next", attribute_index)
            else:
                chains.set(face.index, "attributes_features", attribute_index)
            if earlier:
                chains.set(earlier, "next_of_type", attribute_index)
            else:
                first = attribute_index
            earlier = attribute_index

        if not head:
            _note_in_list(chains, body, first)

    return chains.applied(nodes)

