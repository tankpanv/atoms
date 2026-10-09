# Optional build tools

The home and project composers offer Tools → 浏览器验收. It defaults to unchecked. Home preferences survive reload; project preferences are persisted through the owner-scoped PATCH /api/projects/{id}/tools endpoint. Menus close on outside click or Escape.

`enabled_tools` is validated as a list containing only `browser_check`. Project creation and each message transmit the selection; the project, message and immutable job snapshot retain it. Omitted message selections inherit the project preference. Changes apply to subsequent builds, leaving an already running job's original snapshot intact.

Planning and execution receive the snapshot. When unchecked, browser_check is absent from the model's tools and unsolicited browser_check calls are skipped without execution. Browser evidence is not mandatory for completion; business/command/API verification still applies. Final delivery and stabilization use real service startup and HTTP checks without generating a browser scene or starting Chromium. Build and artifact validation remain active. The checkpoint explicitly records that browser interactions were not verified.

Legacy saved plans can resume after changing the tool preference without discarding task progress. Delivery cache identities include the preference so a previous browser report cannot substitute for a new verification policy. When checked, the existing browser workflow and final browser acceptance are available.
