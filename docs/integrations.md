# Sharing installed tools

Hermes Guests uses Hermes's effective Telegram tool selection, including its native plugin
defaults, saved disables, MCP aliases, and availability checks. It does not treat
a service name or plugin manifest as a permission boundary.

The `guests_discover` tool returns the available catalog and actual schemas, with
search and pagination for large installations. The agent can use its existing
owner tools to discover resource IDs and labels. It then proposes selected tools,
input constraints, and an exact expiry using `guests_prepare_access`.

## Generic permission contract

Each selected tool has a real name and constraints for its inputs. Supported rules
are exact scalar/array values, allowed values, numeric ranges, and explicit any-value
permissions. Omitted inputs are not granted. Fixed values and permitted documented
defaults can be supplied automatically before signing the exact dispatched call.
Required inputs must have explicit rules. Arrays may be pinned as complete values. Whole-object equality is not expressible
by this SDK and is rejected before confirmation; the agent can use separated
resource fields or explicitly propose broader input access. This is not a general
nested-policy language.

Trusted code snapshots the schema, validates the rules, compiles the grant through
the released Tenuo SDK, and renders its English review from the same normalized
object. The model's independent summary cannot replace that review. The owner
alone confirms guest root issuance or changes. No model-callable approval tool
or access to signing keys exists.

Guest proxy schemas expose only approved inputs and choices. Every actual call
is checked again locally. A changed or unavailable tool blocks calls and requires
fresh discovery/review; it does not inherit permission by keeping the same name.

## Separate operation groups

Repeat a tool selection with a complete constraint set for each allowed operation
group. For example, one rule grants lock/unlock on Door B, and a second grants
on/off on Lights C, D and E. Choices within a group can combine. Fields from
different groups cannot combine. The same normalized groups drive the review,
proxy schema and signed constraint. Existing single-rule saved grants retain their
representation.

Internally, grouped proxies accept `{"request": [{...native arguments...}]}`. The
singleton list lets the released SDK evaluate a generated whole-operation CEL
constraint. It is compiled from the validated typed rules. The model cannot submit
CEL. Every field in the selected group is required, and additional inputs are
denied. Grouped proxies do not fill defaults.

The signed call evidence binds that wrapped request. After authorization, trusted
code validates and unwraps it, then calls the original native handler with its
original argument shape. Evidence declares `argument_view: single_request`.
An independent receiver would need to verify this declared argument mapping. The
guaranteed enforcement point remains the local Hermes Guests dispatcher. A malformed
wrapper that passes SDK evaluation is still blocked by the adapter before dispatch
and recorded separately from an SDK denial.

A child may select/narrow complete parent groups. Its generated constraint conjoins
the parent expression with the narrower expression for SDK-verified attenuation.
Root changes follow normal owner review, replacement and signed revocation.

## Human permission descriptions

A tool selection may include `presentation` with action/resource field names,
readable input labels and names for the exact selected resources. This lets the
sharing skill describe calendars, research services, media and other MCP tools
through the same mechanism. Resources retain their canonical IDs in the card.

Trusted code validates these bindings and derives the card from the normalized
constraints. Every remaining input restriction is rendered even when the proposal
omits its label. A redundant category can be folded into the resource description
only when its exact value is already present as the literal prefix of every
selected resource ID. Separate argument rules retain separate action/resource
statements. Arbitrary summary text is rejected.

The labels express the agent's interpretation of the discovered tool. Their
semantic accuracy still needs review against the actual integration. For grouped actions, the review includes the tool’s own description when
it adds detail to the action label. Descriptions longer than 300 characters are
shortened and marked with an ellipsis. Input names appear beside custom labels
when the label omits the input name. If a review restores ended access, every
message with an approval button names the Telegram account. A review cannot
certify hidden service behavior. Metadata alone grants no executable authority.
Older saved grants keep their exact constraints and receive an explicit fallback
review. Descriptions survive restart and owner-approved renewal.

## Helpful service guidance

A plugin can provide resource discovery tools, useful parameter titles and
descriptions, and service-specific guidance through its normal schemas and skills.
Hermes Guests does not require a new metadata convention to work with a custom plugin.
Optional adapters can improve the precision and language of resource permissions.
The Home Assistant and web research presets are such examples, not the product's
fixed tool universe.

A schema describes inputs, not all hidden side effects or returned data. For
unfamiliar tools the confirmation states that limit. If a requested restriction
cannot be expressed, the agent proposes the closest enforceable scope or declines
that part. Arbitrary host execution, filesystem access, memory, cron, and authority
management are excluded from generic sharing rather than disguised as safe scopes.

## Native runtime seam

The package uses public native plugin registration, state, Telegram handlers,
supervised tasks, profiles, and tool registry dispatch. Effective platform tool
selection is isolated in `hermes_guests.compat`, which calls the same resolver as
Hermes's gateway; Hermes currently exposes that resolver as an internal helper.
If it disappears, Hermes Guests fails setup with an update message instead of reconstructing
or broadening the catalog. This seam is covered by native compatibility tests.
