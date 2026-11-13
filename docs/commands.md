# Command reference

After setup, tell Hermes who should get access, what they can do and for how long.
You can inspect, change and end access conversationally. These commands provide
direct access to the same controls.

## Owner commands

Send these in your private Telegram chat with the bot.

| Command | What it does |
| --- | --- |
| `/guests_setup` | Set up sharing: the tools you're willing to share, the never-share list, and how long guest data is kept after access ends. Run again to change any of them or to approve newly connected tools. |
| `/guests` | See who has access, pending reviews and invitations. Buttons: **Add a guest**, **Check sharing**, and **Activity** for everyone's recent activity. Open a guest for **Change what they can use**, **Extend**, **Activity**, **End access** and **End and delete data**. **Extend** offers increments that fit within 30 days from now, or no end date. Every change shows the full review first. |
| `/guests_new <name>` | Add a guest with buttons: chat only or chosen tools, then how long, then the review. |
| `/cancel` | Cancel a pending typed answer, such as a word for the never-share list. |

`/guests_recover` resets the sharing identity after unrecoverable key loss; see
[Setup and recovery](setup-recovery.md#lost-sharing-identity).

Your ordinary Hermes commands remain available in your owner chat.
[Setup and recovery](setup-recovery.md)

## Guest commands

| Command | What it does |
| --- | --- |
| `/help` | Show current permissions and access controls. |
| `/access` | Show approved permissions, end time and access status. |
| `/renew` | Request more time for active, time-limited access. Expired guests can use the renewal button. The owner reviews the request. |
| `/new` | Start a fresh conversation, keeping current permissions and authorized saved preferences. |
| `/cancel` | Cancel a pending text request for more access. |
| `/start` | Open the invitation when following its link, or show the guest's permissions once joined. |

Guests can also say “What can I do?” or “Request access to …”, and use
**Request more access** or **Request more time**. Access changes require owner
approval. A guest can have one request open at a time and make three a day, for
more access and for more time alike. After access is ended, the owner can send a
new invitation.

## Never-share list

Tools and words that no guest may ever use, whatever their access allows. Open it
from `/guests_setup`, or ask Hermes, for example “never let guests unlock the front
door” or “allow kitchen again”. A change asked for in words is applied only after
you press **Confirm**. Changes apply at once to existing and new access.

You can block a whole tool, or a word or phrase everywhere it could appear, in one
tool's inputs, or in one input of a tool. Matching normalizes case, spacing and
look-alike characters, and includes longer text containing the value. Short words
can therefore block more than intended. This matches input text and resource
identifiers. It cannot identify paraphrases or filter returned data.
