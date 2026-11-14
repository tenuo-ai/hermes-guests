# Hermes Guests

Let friends and teammates use your Hermes through Telegram. Choose what they can
do and how long they have access. They only need Telegram.

> “Let Alex use my research tools until tomorrow.”
>
> Hermes shows you the permissions to review. Approve, send Alex the invitation,
> and confirm their Telegram account when they join.
>
> Alex can start asking questions in their own chat with your bot.

For a chat-only invitation, try “Let Alex chat for a day with no tools.”

## Get started

You'll need [Hermes](https://github.com/NousResearch/hermes-agent) with a working
model and a Telegram bot. Set up the bot so your Telegram account is its only
allowed user. Hermes Guests handles guest invitations.

**1. Install Hermes Guests** in the environment that runs your gateway.

```sh
hermes plugins install tenuo-ai/hermes-guests --enable --yes-deps
```

Review any source-scan findings. Follow Hermes's reload prompt, or restart the
gateway if your deployment manages it separately.
[Installation help](docs/setup-recovery.md#installation-help)

**2. Set up sharing when you're ready.** Send a message in your private Telegram
chat. Hermes replies as usual and offers **Set up guest sharing** once. You can
ignore it and keep using Hermes. When you're ready, ask Hermes to share something
or send `/guests_setup`. Review the setup and choose **Enable sharing**.

**3. Say what you'd like to share.** Use tools you've already connected to Hermes.

> “Let Alex use my research tools for two days. Give them a day to accept.”

Check the permission review, then choose **Approve**, **Change** or **Cancel**.
Send the invitation to Alex and confirm the account that claims it. Alex can then
chat with the bot using the access you approved.

## Keep it conversational

You can manage sharing from your own chat.

| What you want | Try saying |
| --- | --- |
| See current access | “Who has access?” |
| Change permissions | “Let Alex control the porch light too.” |
| Give more time | “Extend Alex's access until Sunday.” |
| End access | “End Alex's access.” |
| Check activity | “What did Alex do today? Show the times in my timezone.” |
| Delete a guest's data | “End Alex's access and delete their data.” |
| Never share something | “Never let guests unlock the front door.” |
| Keep guest data for a set time | “Delete guest data a week after access ends.” |

Permission changes, never-share changes and deletion require your confirmation.
Guests can inspect their permissions and request more access or time from their
own chat.

Each guest has separate conversations and private memory. Ending access stops
further model use and tool calls, while keeping their data for a possible return.
Data is kept until you delete it by default; you can choose automatic deletion in
`/guests_setup`, and each new guest's review says which applies.

Prefer buttons? `/guests` shows who has access, with **Add a guest**, **Check
sharing** and **Activity**. Open a guest to change what they can use, **Extend**
their access, see their activity or end it. `/guests_new <name>` adds a guest step
by step. See the
[command reference](docs/commands.md).

## How access works

Every guest needs a valid signed warrant to chat or call a tool. The warrant
limits what they can do and when access ends. Even chat-only access requires a
warrant. Hermes Guests issues an owner warrant too. Your ordinary Hermes tools
keep working if that warrant is missing or invalid. You can opt into requiring
an owner warrant in [advanced setup](docs/setup-recovery.md#owner-warrants).
[Authorization details](docs/authorization.md)

This preview supports one owner and Telegram private text chats. Guests use your
model quota. Their messages go to Telegram and your configured model. Approved
tool calls send their inputs to the service you shared. Hermes stores guest
conversations, memory, and access records locally in separate profiles.

[Model setup](docs/setup-recovery.md#model-setup) ·
[Security and limits](docs/advanced.md)

## Contribute

Try the [sharing scenarios](docs/scenarios.md),
[report an issue](https://github.com/tenuo-ai/hermes-guests/issues), or read
[CONTRIBUTING.md](CONTRIBUTING.md) to work on the code.

Licensed under [Apache-2.0](LICENSE).
