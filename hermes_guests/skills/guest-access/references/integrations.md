# Integration discovery for proposals

Use actual installed tool names, input schemas, resource discovery, handler
semantics when available, and stable service/resource IDs. A plugin being official
or a schema being valid does not certify the proposed boundary.

Structured integrations: resolve friendly labels to real resource IDs, select
required operations, constrain their arguments, and validate output scope.
For Home Assistant, check exact domain/service/entity combinations, optional data,
and indirect scene/group targets. Do not grant enumeration of unrelated devices.

Custom plugins: inspect their schemas and available implementation evidence.
Optional plugin-provided hints improve the proposal but are not trusted authority.
If a generic API tool accepts arbitrary method/path/body, do not call an HTTP
method restriction a guarantee about the complete operation.

If the requested narrow boundary cannot be expressed, tell the owner exactly what
the available tool permits and propose the closest enforceable scope. A best-effort
proposal must still be validated before activation; enforcement never becomes
best-effort because inference was uncertain.

Do not require the owner to learn tool schemas or edit policy. Ask a short
product-level clarification only when needed.


## Resource-oriented review labels

New proposals should include presentation bindings and verified resource names.
For a tool with an action input, bind that actual input through `presentation.action`.
For a resource reader with no action input, use a short `presentation.operation`
such as “Read calendar events”. These labels interpret tool semantics and must be
checked against discovery. They grant no authority. The renderer groups exact
resource selections across tools and keeps every remaining input restriction
under its applicable operation. Broad or legacy selections retain explicit reviews.


## Private-address exceptions

An owner-approved exact URL exception applies only to the input and complete
permission rule that selected it. A URL quoted in one input grants no exception
for a different destination input. Agent-selected child scopes cannot create
owner approval for private addresses. Use the current owner-reviewed root and
show the actual selected destination in the permission review.
