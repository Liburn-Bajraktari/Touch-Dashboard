# Enabling Local IPC in Vesktop

If you are using **Vesktop** instead of the official Discord client, Touch Dashboard's Discord integration (Mute/Deafen controls, voice state display) might not connect automatically. 

Vesktop requires the **Local IPC (Inter-Process Communication)** server to be explicitly enabled in its settings for external applications to connect to it.

## How to Enable IPC in Vesktop

1. Open Vesktop.
2. Go to **Settings** (the gear icon).
3. Scroll down to the **Vencord** section.
4. Click on **Open Settings Folder**.
5. In the folder that opens, open the `settings.json` (or `settings/settings.json`) file in any text editor.
6. Ensure that both `"discordIpc"` and `"arRPC"` are set to `true`.

Your `settings.json` should contain lines looking like this:

```json
{
  "discordIpc": true,
  "arRPC": true
}
```
*(There will be other settings in the file, just ensure these two are present and true).*

7. **Save the file** and **restart Vesktop**.

Once Vesktop is restarted, the IPC socket will be active, and Touch Dashboard will automatically discover and connect to it!
