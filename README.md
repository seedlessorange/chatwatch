# chatwatch

A small local page that shows how much of your Claude plan each open Claude Code chat is using, so you can tell which one to close, restart or move to a lighter model. It works with Pro and Max plans, on a Mac or on Windows.

Claude Code tells you how much of your weekly limit and your 5-hour window you have used, but not which chat used it. chatwatch reads the transcripts Claude Code already keeps on your computer and splits the meter between your chats. It runs in the background, sends a notification when something needs a look, and has a button that closes a chat after asking it to write a handoff note for a fresh one.

## Install

You need Python 3.8 or newer and Claude Code signed in with a Claude subscription. Then, on a Mac:

```sh
git clone https://github.com/seedlessorange/chatwatch ~/chatwatch
~/chatwatch/install.sh
```

On Windows, in PowerShell:

```powershell
git clone https://github.com/seedlessorange/chatwatch $HOME\chatwatch
cd $HOME\chatwatch
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The Windows version has not been tested on a Windows machine yet. If the installer or the page fails, open an issue with the error text and it will get fixed.

Either installer starts chatwatch at login and opens http://127.0.0.1:7731. On a Mac, the first reading may ask whether `security` can read the "Claude Code-credentials" Keychain item; that is how chatwatch reads your plan's meters (see below). On Linux there is no installer: run `python3 chatwatch.py serve` however you like to keep things running.

## The menu bar app (Mac)

On a Mac the installer also builds a small menu bar app if Xcode's command line tools are installed (`xcode-select --install`; run `mac/build.sh` afterwards if you add them later). It shows a ring and the percentage of the limit you are closest to, and turns red when you are past 90% or when the last hour's pace would reach the limit before it resets. Click it for the same chats and close buttons as the page, without opening a browser. If you have many menu bar icons, a new one can end up hidden behind the notch; hold Command and drag it further right.

## What you see

At the top, your weekly limit and your 5-hour window, the same percentages `/usage` shows, with the last hour's use drawn as a striped band and a warning if that pace would hit the limit before it resets. Below, one card per open chat, under the same title as its tab: how much of the limit it used in the last hour and the last day, how much context it holds, what its next step will cost, which model it is on and how long it has been idle, with a suggestion when there is one (start fresh, close it, or switch models).

Each chat's numbers are shown against whichever limit you are closer to. On Pro that is usually the 5-hour window.

Close + handoff asks a throwaway copy of the chat for a handoff note on the same model, saves it in `~/.claude/handoffs/`, stops the chat and puts a line on your clipboard to paste into a new chat. That one step costs about what the card's "next step" figure says. Close without a handoff stops the chat straight away. In VS Code the tab stays open but no longer answers, and `claude --resume <id>` in the same folder brings any closed chat back.

A model can only be changed from inside the chat, by typing `/model sonnet` there. chatwatch can suggest it but cannot do it for you.

You can also get a table in the terminal with `python3 chatwatch.py`.

## Alerts

At most one notification every 5 minutes, listing only what is new: a chat holding more than 250K tokens of context, a chat using 2% of your limit in an hour, a pace that reaches the limit before it resets, a Fable chat left idle for 30 minutes, or ten chats idle for three hours. The same alert for the same chat repeats at most every six hours. The thresholds are at the top of `chatwatch.py`.

## How the per-chat numbers are worked out

Anthropic reports one percentage per limit and does not say which chat used it. Every 5 minutes chatwatch takes a reading of the meters and adds up what all your chats used since the previous reading, weighting each step by its model's API price, so a step on Opus counts for more than one on Sonnet. Dividing how far the meter moved by that total gives how much of the limit one unit of usage is worth, and each chat's share follows from that. The figure needs the meter to move a few points first, so after installing you will see dollars until it has.

On one Max account, two separate stretches of a day each came out within 15% of each other, which is about as close as the whole-number meter allows. Treat the per-chat shares as good estimates. Anything you do on claude.ai or the Claude apps is not in these transcripts, so it gets spread over your chats.

The dollar figures are what the same tokens would cost on the API. Nothing is billed; they are there to compare chats.

## What it reads and sends

chatwatch reads `~/.claude/sessions/` and `~/.claude/projects/` (the files Claude Code writes) and never changes them. To read the meters it takes Claude Code's own login from the macOS Keychain or from `~/.claude/.credentials.json`, sends it to `api.anthropic.com` only, for that one request, and never stores or logs it. It never refreshes the login. That address is the one Claude Code itself uses for `/usage`, and it is not a public API, so Anthropic could change it; if that happens chatwatch falls back to dollars.

The page is served on 127.0.0.1 only. The close buttons need a key that is generated each time chatwatch starts and is only in the page, so another website cannot close your chats.

It writes `~/.claude/chatwatch-*.json*` (its readings, calibration and alert history), `~/.claude/handoffs/` and, on a Mac, `~/.claude/chatwatch.log`.

## Remove it

On a Mac, `~/chatwatch/install.sh --uninstall`. On Windows, `powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall`. Then delete the folder.

MIT licence.
