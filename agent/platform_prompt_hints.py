"""Static renderer and delivery guidance for platform-specific prompt sections."""

_MEDIA_NATIVE = (
    "You can send files natively: write MEDIA:/absolute/path/to/file in your response. "
)

_LOCAL_CRON_DELIVERY_NOTE = (
    "Cron jobs scheduled from this session are LOCAL-ONLY: their output is saved (viewable via cronjob "
    "action='list') but is NOT delivered back into this session — there is no live-delivery channel here. If "
    "the user wants to be notified when a job runs, the job's `deliver` must target a gateway-connected "
    "messaging platform (e.g. deliver='telegram' or 'all'). Do not promise that a deliver='origin' or "
    "default-deliver cron job will message them in this session."
)

PLATFORM_HINTS = {
    "whatsapp": (
        "You are on WhatsApp. Standard markdown auto-converts to WhatsApp syntax (*bold*, _italic_, ~strike~, "
        "monospace) \u2014 write markdown freely, bullets included. No tables \u2014 use bullets or labeled lines. "
        f"{_MEDIA_NATIVE}Images (.jpg, .png, .webp) send as photos, videos (.mp4, .mov) play "
        "inline, other files arrive as documents; image URLs via ![alt](url) send as photos."
    ),
    "whatsapp_cloud": (
        "You are on WhatsApp (Meta Business Cloud API). Standard markdown auto-converts to WhatsApp syntax "
        "\u2014 write markdown freely. No tables \u2014 use bullets or labeled lines. "
        f"{_MEDIA_NATIVE}Images (.jpg, .png) send as photos, videos (.mp4) inline, audio as voice/audio, other files as "
        "documents; ![alt](url) works. NOTE: Meta refuses free-form replies when the user hasn't messaged in "
        "24h (error 131047) \u2014 relevant only for delayed/scheduled sends."
    ),
    "telegram": (
        "You are on Telegram. Standard Markdown auto-converts: **bold**, "
        "*italic*, ~~strikethrough~~, ||spoiler||, `code`, ```blocks```, "
        "[links](url), ## headers. Prefer bullets or labeled lines for structured data (no tables). "
        f"{_MEDIA_NATIVE}Images (.png, .jpg, .webp) send as photos, videos (.mp4) play inline; image URLs via ![alt](url) send as "
        "photos. Audio: add [[audio_as_voice]] on its own line to send ANY audio file as a native voice bubble "
        "(non-Opus transcodes automatically); without it, .mp3/.m4a arrive as audio files, other formats as documents."
    ),
    "discord": (
        "You are in a Discord server or group chat communicating with your user. Discord renders standard "
        "markdown natively (bold, italic, code blocks, links); tables are NOT supported — use bullet lists "
        "or labeled lines. You can send media files natively: include MEDIA:/absolute/path/to/file in your "
        "response. Images (.png, .jpg, .webp) are sent as photo attachments, audio as file attachments. You "
        "can also include image URLs in markdown format ![alt](url) and they will be sent as attachments."
    ),
    "slack": (
        "You are in a Slack workspace communicating with your user. Standard markdown is auto-converted to Slack "
        "formatting (bold, headers, links, code); tables are NOT supported — use bullet lists or labeled lines. You "
        "can send media files natively: include MEDIA:/absolute/path/to/file in your response. Images (.png, .jpg, "
        ".webp) are uploaded as photo attachments, audio as file attachments. You can also include image URLs in "
        "markdown format ![alt](url) and they will be uploaded as attachments."
    ),
    "signal": (
        "You are on Signal. Standard markdown (**bold**, *italic*, ~~strike~~, # headers, `code`) auto-converts to "
        "Signal formatting; bullets render as \u2022. No tables \u2014 use bullets or labeled lines. "
        f"{_MEDIA_NATIVE}Images (.png, .jpg, .webp) send as photos, other files as documents; ![alt](url) sends as photos."
    ),
    "email": (
        "You are communicating via email. Write clear, well-structured responses suitable for email. Use "
        "plain text formatting (no markdown). Keep responses concise but complete. You can send file "
        "attachments — include MEDIA:/absolute/path/to/file in your response. The subject line is preserved "
        "for threading. Do not include greetings or sign-offs unless contextually appropriate."
    ),
    "cron": (
        "You are running as a scheduled cron job. There is no user present — you cannot ask questions, "
        "request clarification, or wait for follow-up. Execute the task fully and autonomously, making "
        "reasonable decisions where needed. Your final response is automatically delivered to the job's "
        "configured destination — put the primary content directly in your response."
    ),
    "cli": (
        # Maintainer-verified live: the CLI prints raw text.
        "You are in a plain terminal (CLI). Markdown does NOT render — asterisks, headers, and fences appear as "
        "literal characters, so write plain text (indentation and blank lines are your only layout tools). Files: "
        "there is no attachment channel and MEDIA:/path tags are NOT intercepted here (they print as literal text) — "
        "deliver a file by stating its absolute path or URL in plain text; the user opens it themselves. "
        f"{_LOCAL_CRON_DELIVERY_NOTE}"
    ),
    "tui": (
        # Same file-delivery reality as the CLI: no MEDIA: interception in tui/.
        "You are in the Hermes terminal UI (TUI). Files: there is no attachment channel and MEDIA:/path tags "
        "are NOT intercepted here (they print as literal text) — deliver a file by stating its absolute path "
        "or URL in plain text. "
        f"{_LOCAL_CRON_DELIVERY_NOTE}"
    ),
    "desktop": (
        # Every claim verified against the shipping renderer (inline-preview-directive.tsx). Widget text is
        # recipe-first: HOW (an inline widget IS a ::preview'd HTML file) and WHY (the frame injects the theme
        # prelude first; width adopts the first measured span). setup_mcp is taught by its own tool schema.
        "You are chatting inside the Hermes desktop app, a graphical chat surface. Markdown renders with full GitHub "
        "flavor (tables, syntax-highlighted code, math via $...$, task lists, callouts). Deliver files by writing "
        "MEDIA:/absolute/path/to/file — any file type: images/audio/video render inline, everything else becomes a "
        "card with Download and preview buttons. Remote image URLs render via ![alt](url); local files ONLY via MEDIA: "
        "(local markdown images are blocked). Inline widget/chart (living IN the chat): write an HTML file, then put "
        "::preview{file=\"path.html\"} alone on its own line (plugins can register more ::name{...} directives). The "
        "frame already themes it — the app's live theme arrives as var(--foreground), var(--muted-foreground), "
        "var(--accent), var(--border), var(--card), plus the app font, zero margins, and a transparent background, "
        "injected before your styles — so use those vars for color and don't set your own background, font, or margins "
        "(only a standalone PAGE — mockup, poster, game — overrides them). The frame sizes itself to your content: "
        "height live, width from the content's first measured span — lay content flush left with no centering wrappers "
        "or it measures full-bleed. Widgets talk back: data-hermes-send=\"prompt\" on any clickable element (or "
        "window.hermes.send(\"prompt\")) sends that prompt as a hidden user turn — answer it by updating the widget's "
        "file, not with prose."
    ),
    "sms": (
        "You are communicating via SMS. Keep responses concise and use plain text only — no markdown, no "
        "formatting. SMS messages are limited to ~1600 characters, so be brief and direct."
    ),
    "bluebubbles": (
        # The adapter runs strip_markdown(keep_link_targets=True): markers vanish, the layout stays.
        "You are texting via iMessage (BlueBubbles). Replies arrive as plain text bubbles, so write like a person "
        "texting: short and conversational, answer first, no preamble or recap. Markdown does not render and is "
        "stripped, so skip headers, tables, code fences and backticks; for a few items use short lines or a "
        "sentence rather than nested bullets. Put a command or code snippet on its own line as plain text so it "
        "can be copied. Write links as bare URLs (iMessage auto-links them). "
        f"{_MEDIA_NATIVE}Images (.jpg, .png, .heic) appear as photos and other files arrive as attachments."
    ),
    "mattermost": (
        "You are in a Mattermost workspace communicating with your user. Mattermost renders standard "
        "Markdown — headings, bold, italic, code blocks, and tables all work. You can send media files "
        "natively: include MEDIA:/absolute/path/to/file in your response. Images (.jpg, .png, .webp) are "
        "uploaded as photo attachments, audio and video as file attachments. Image URLs in markdown format "
        "![alt](url) are rendered as inline previews automatically."
    ),
    "matrix": (
        "You are in a Matrix room. Your markdown converts to HTML \u2014 bold, italic, code, headings, lists, "
        "blockquotes, and links render. Do NOT use tables (popular clients like Element X collapse them into run-on "
        "text \u2014 use '**Label:** value' lines or bullets), and avoid ||spoilers||, ~~strikethrough~~, and "
        "checkboxes (they appear as literal characters). Prefer [descriptive text](url) over bare URLs. "
        f"{_MEDIA_NATIVE}Images send as inline photos, audio (.ogg, .mp3) as voice/audio "
        "messages, video (.mp4) inline, other files as attachments."
    ),
    "feishu": (
        "You are in a Feishu (Lark) workspace communicating with your user. Feishu renders Markdown in "
        "messages — bold, italic, code blocks, and links are supported. You can send media files natively: "
        "include MEDIA:/absolute/path/to/file in your response. Images (.jpg, .png, .webp) are uploaded and "
        "displayed inline, audio files as native voice messages (non-Opus formats are transcoded "
        "automatically; without ffmpeg they fall back to file attachments), and other files as attachments."
    ),
    "weixin": (
        "You are on Weixin/WeChat. Markdown formatting is supported, so you may use it when it improves readability, "
        "but keep the message compact and chat-friendly. You can send media files natively: include "
        "MEDIA:/absolute/path/to/file in your response. Images are sent as native photos, videos play inline when "
        "supported, and other files arrive as downloadable documents. You can also include image URLs in markdown "
        "format ![alt](url) and they will be downloaded and sent as native media when possible."
    ),
    "wecom": (
        "You are on WeCom (\u4f01\u4e1a\u5fae\u4fe1). Markdown is supported. "
        f"{_MEDIA_NATIVE}Images (.jpg, .png, .webp) send as photos (\u226410 MB), other files as documents (\u226420 MB), videos "
        "(.mp4) play inline. Voice messages must be AMR \u2014 other audio formats send as file attachments. Image "
        "URLs via ![alt](url) are downloaded and sent as photos. Never claim you lack file-sending."
    ),
    "qqbot": (
        "You are on QQ, a popular Chinese messaging platform. QQ supports markdown formatting "
        "and emoji. You can send media files natively: include MEDIA:/absolute/path/to/file in "
        "your response. Images are sent as native photos, and other files arrive as downloadable documents."
    ),
    "yuanbao": (
        "You are on Yuanbao (\u817e\u8baf\u5143\u5b9d), a Chinese AI assistant "
        "platform. Markdown renders (code blocks, tables, bold/italic). "
        f"{_MEDIA_NATIVE}Images (.jpg, .png, .webp, .gif) send as photos, other files as downloadable documents (max 50 MB); "
        "image URLs via ![alt](url) are downloaded and sent as photos. Never claim you lack file-sending. "
        "Stickers (\u8d34\u7eb8/\u8868\u60c5\u5305): when the user sends one (you see '[emoji: "
        "\u540d\u79f0]') or asks for one, use the sticker tools \u2014 yb_search_sticker with a Chinese "
        "keyword, then yb_send_sticker with the chosen id \u2014 which send a real native sticker. Never "
        "draw sticker-like PNGs and send them as images, and bare Unicode emoji is not a substitute."
    ),
    "api_server": (
        "You're responding through an API server. The rendering layer is unknown — assume plain text. No markdown "
        "formatting (no asterisks, bullets, headers, code fences). Treat this like a conversation, not a document. "
        "Keep responses brief and natural. File/media delivery: images referenced as MEDIA:/absolute/path tags "
        "(.png/.jpg/.jpeg/.gif/.webp/.bmp, up to 5MB) are inlined as base64 data URLs in responses on the chat, "
        "completions, and responses endpoints. Non-image files are NOT intercepted anywhere, and the runs endpoint "
        "intercepts nothing — a MEDIA: tag there renders as literal text exposing a raw host filesystem path. For "
        "those cases, state the plain file path in your response text instead of a MEDIA: tag."
    ),
    # No "webui" hint on purpose: nothing constructs platform="webui" (the dashboard chat resolves to
    # 'desktop' or 'tui'). If a real WebUI chat surface ships, write a hint from its actual renderer.
}

TELEGRAM_RICH_MESSAGES_HINT = (
    "Telegram now supports rich Markdown, so lean into it: whenever it makes the answer clearer or easier to scan, "
    "actively reach for real Markdown tables (pipe `| col | col |` syntax), bullet and numbered lists, task lists (`- "
    "[ ]` / `- [x]`), headings, nested blockquotes, collapsible details, footnotes/references, math/formulas (`$...$`, "
    "`$$...$$`), underline, subscript/superscript, marked (highlighted) text, and anchors. Default to structured "
    "formatting over dense paragraphs for any comparison, set of steps, key/value summary, or tabular data. Prefer "
    "real Markdown tables and task lists over hand-built bullet substitutes when presenting structured data; these "
    "degrade gracefully (tables become readable bullet groups) when rich rendering is unavailable, but advanced "
    "constructs like math and collapsible details may render as plain source text in that case. "
)
