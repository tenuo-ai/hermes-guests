# Sharing scenarios

Install Hermes Guests into your own Hermes by following the repository README. Use your
Telegram bot and invite a friend or teammate. Installation, setup and the first
invitation are part of the experience we want feedback on. [README](../README.md#get-started)

Choose a scenario that matches what you want to share and the tools you already
have connected. Chat-only sharing works with your existing model and Telegram
connection. Mark unavailable scenarios as untested.

## Getting started

Can you follow the README, enable sharing and invite someone without help? Were
the never-share list and guest data settings at the end of setup clear?
Check that the review describes what you intended, and that the guest understands
who invited them, what they can do and when their access ends. Let each person
explain the permissions in their own words before trying a task.

## Everyday sharing

### Friends chatting

Ask Hermes to let a friend chat for a day with no shared tools. Invite them and
have a useful conversation. Ask for an action you did not share, such as opening
your files. Check that your information and connected services stay private.

### Different permissions for teammates

Use a connected tool with resource controls, such as a company, project or library.
Give Alex access to one resource and Blair another. Have both use their own chats.
Each should complete a useful allowed task. Try the other person's resource and
check the actual service outcome. Also try a limit you selected, such as the number
of results. Check that the conversations stay separate.

### Family calendars, reminders and research

Share a selected calendar, reminders list or public research capability that you
already use. Ask the guest to do something useful, then try another calendar or
list. If your calendar tool supports excluding private events, include that in the
request and check the result. Record any restriction the review cannot express.

### Selected home controls

If you have home-control tools, share selected actions on resources you choose.
For example, allow a door and three lights, or temperature reading and light control.
Check that the review makes each resource's actions clear. Try an allowed action,
another device and an action that belongs to a different resource. Check what
actually happened in the connected service.

### Things no guest may use

In `/guests_setup`, open the never-share list and block a tool or a word, such as
a door lock or a room. Then ask Hermes in words to never share something else and
press **Confirm**. Try to share a blocked item with a guest, and have an existing
guest ask for it. Check that both are refused, that the review says what was left
out, and that nothing happened in the connected service.

## Private guest memory and data lifecycle

### Remember each person

Have a guest ask Hermes to remember a preference and a distinctive project name.
Start a fresh conversation with `/new`, then ask about them. Repeat after a normal
Hermes restart. A second guest should have their own conversation and preferences.
Check that neither guest receives the owner's or the other guest's information.

### Change, extend and end access

Ask Hermes to change a guest's permissions. Check the complete review and the
message the guest receives. Try an added permission and one you removed. Request
more time as the guest, and separately use **Extend** on the guest in `/guests`;
check that each takes effect only after owner approval. Try a short access
period and check what happens when it expires.

End the guest's access, then have them ask for an ordinary task such as writing a
story, as well as a shared-service action. Both should be refused. Reauthorize the
same guest and check that their saved preferences are available again.

### Keep or delete guest data

Open Guest data from `/guests_setup`. The default should keep data until you delete it,
and a new guest's review should say so.
Ending access should preserve the guest's conversation and preferences. If you
choose automatic deletion, review the period and which retained data it affects.

For a guest created for this test, choose End and delete data. Cancel once
and check that their access still works, then confirm deletion. Check that the
status explains whether deletion is pending or complete. Invite that person again
and check that their new conversation starts without the deleted preferences.

## Everyday recovery and phone use

Try changing or canceling a review, requesting more access and recovering an
expired or lost invitation. If you choose a custom invitation window, check that
the displayed deadline matches your request. Check the controls on your phone,
including enlarged text if you use it. Record anything confusing or hard to find.

Ask for your own activity, a guest's activity and everyone's activity in your
preferred timezone, and also use the **Activity** buttons in `/guests`. Check that each entry has a readable date and time, identifies
who acted, and makes the recorded outcome understandable.

## Record what happened

For each case, share a short report.

- What you tried and which connected service you used
- What you expected and what actually happened
- Whether the owner and guest understood the permissions
- Any unclear wording, missing controls or steps requiring help
- Pass, fail or untested, with a redacted screenshot when helpful
- Hermes version and Hermes Guests revision, if available

Record what you observed in the chat and connected service. Engineers can use
[development testing](testing.md) to investigate authorization and model-request
behavior. Current support covers one owner and Telegram private text chats.
