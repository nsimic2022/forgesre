import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MessagePreviews, type Previews } from "./MessagePreviews";

const slack = { text: "[HIGH] Opened: Disk", attachments: [{ color: "#ea580c", blocks: [
  { type: "section", text: { type: "mrkdwn", text: "*[HIGH] Opened: Disk /u02 92%*" } },
  { type: "section", fields: [{ type: "mrkdwn", text: "*Host*\ndb-ora-02" }, { type: "mrkdwn", text: "*Incident*\nINC-4402" }] },
] }] };
const data: Previews = {
  incident: "INC-4402", public_url: "", note: "",
  alert_email: { subject: "Alarm db-ora-02", text: "Incident: INC-4402", html: "<html><body>mail</body></html>" },
  incident_report: { subject: "Alarm db-ora-02", text: "", html: "<html><body>report</body></html>" },
  chat: { events: { opened: { text: "[HIGH] Opened: Disk", payloads: { slack, teams: {}, discord: {} } }, escalated: { text: "", payloads: {} }, resolved: { text: "", payloads: {} } },
    channels: { slack: "Slack", teams: "Microsoft Teams", discord: "Discord" }, active: ["slack"] },
};

describe("MessagePreviews", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows the alert mail, warns about the public address and draws the chat card", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<MessagePreviews />);
    expect(await screen.findByTitle("Alert mail preview")).toBeTruthy();
    expect(screen.getByText("Alarm db-ora-02")).toBeTruthy();
    expect(screen.getByRole("note").textContent).toContain("Public address is empty");
    fireEvent.click(screen.getByRole("button", { name: "Slack, Teams, Discord" }));
    expect(screen.getByText("[HIGH] Opened: Disk /u02 92%")).toBeTruthy();
    expect(screen.getByText("db-ora-02")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Microsoft Teams (off)" })).toBeTruthy();
  });
});
