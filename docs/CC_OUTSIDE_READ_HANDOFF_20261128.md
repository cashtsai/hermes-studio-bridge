# CC Outside-Directory Read Approval Repair

## Cause

Pocket-branch was alive but waiting at Claude's new four-choice read permission
modal. The parser only recognized questions beginning with "Do you want" and
required Claude's cancel footer to be the last line. The live screen instead
asked "Allow this read outside the working directories?" and had a trailing
`ccs` branded rule. Status therefore reported `blocked: true, prompt: null`.

The inline iOS CC card also collapsed all choices into a Boolean. For this
particular modal, the first Yes or No changes future permissions, whereas choices
3 and 4 apply only to the current request. Detecting this modal without fixing
decision routing would risk changing permanent policy unintentionally.

## Patch

- Recognize the exact new question and the narrow branded footer form.
- Keep bottom anchoring, wrapped labels, full warning, and requested path.
- Mark the prompt and stored approval metadata `requires_explicit_choice`.
- Reject Boolean-only decisions with `EXPLICIT_CHOICE_REQUIRED`.
- Do not advertise binary approval shortcuts in push notifications.
- Match the card to the current prompt and recheck after acquiring the key lock.
- Reject unoffered keys and stale prompts without sending anything.
- Preserve ordinary permission/question behavior and idempotent receipts.

Companion iOS branch: `fix/cc-explicit-approval-choice-20261128` in pocketagent.
It sends `{approval_id, key}` instead of collapsing CC choices into a Boolean.
Existing Approval Center exact-key handling does not need an app update. An old
inline-card client gets an explicit-choice error safely; a Bridge restart alone
cannot update that client.

## Verification

- 244 tests passed across outside-read, permission-modal, CC detection/menu,
  multiselect, blocked status, input delivery, Approval Hub, resolved events,
  and Console approval suites. Includes 18 new regression tests.
- The standalone `test_a3_approval_card_parity.py` reported ALL PASS.
- Python compile and `git diff --check` passed.
- A read-only live Pocket-branch pane parsed all four exact choices using the
  new parser in an isolated test process. No tmux key was sent.
- Production `/health` and `/capabilities` returned HTTP 200.
- Production `/app/v1/messages` dry-run returned HTTP 200 and `[DONE]`.
- The production status still reported `running: true, blocked: true,
  prompt: null`, as expected before deployment. Do not call this live recovery.
- Companion iOS simulator build/test passed: A2DecisionWireTests, three test
  methods including a parameterized check of all four exact keys.

## Release / Recovery Boundaries

Not merged, restarted, or deployed during this repair. Production was f97932e.
The separate open Bridge PR #154 addresses an auto-mode onboarding notice and is
not included here. Reconcile both changes deliberately during release.

No policy file was edited, no permission was granted, and the gated file was not
read by another route. Pocket-branch remains at the original read question until
the user chooses. If the user explicitly allows this read once, re-read the live
prompt, verify it is still the same request, and send only option 4. Do not select
option 1 (persistent allow), option 2 (persistent block), or blindly replay input.

After authorized deployment, verify the existing approval surfaces display all
four choices and the full policy warning. Do not click them as a smoke test.
