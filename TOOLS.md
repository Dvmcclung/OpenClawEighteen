# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.

### File Transfers (SCP to Dale's Laptop)

- **Tailscale address:** `100.81.17.23` (SSH only listens here, not on public IP)
- **Dale's Downloads folder:** `C:\Users\dmcclung.TRANSPORT\Downloads`
- **Template command:**
  `scp qtxit@100.81.17.23:/home/qtxit/<file> "C:\Users\dmcclung.TRANSPORT\Downloads\<file>"`
- Tailscale must be active on Dale's laptop for this to work
- Public IP 187.124.148.51 does NOT have port 22 open
