// The connect dialog: one sentence, one button. `moshi-cockpit connect` runs in a terminal tab and opens moshi's
// page in the browser; this mod never sees a token.

import { div } from "gpui-kit";
import { v_flex } from "gpui-base";
import { home, run, props, closeOverlay, openOverlay } from "herdr";
import { btn } from "./util.js";

export const CONNECT_TITLE = "Connect to moshi";

function connect() {
  run("tab.new", { name: CONNECT_TITLE, argv: [`${home()}/.local/bin/moshi-cockpit`, "connect"] });
  closeOverlay(props().overlay);
}

// From any view of this mod: closes it (when `overlay` is given) and opens the connect dialog instead.
export function openConnect(overlay) {
  if (overlay) closeOverlay(overlay);
  openOverlay({ mod: "mesh-agents", kind: "dialog", width: 420, height: 220, props: { view: "connect" } });
}

export function connectKey(e) {
  if (e.key === "enter") connect();
}

export function connectView(t) {
  return v_flex().size_full().bg(t.popover).p(24).gap(12).items_center().justify_center()
    .child(div().font_semibold().text_size(16).text_color(t.foreground).child(CONNECT_TITLE))
    .child(div().text_size(13).text_color(t.dim).text_center().child("Confirm once in your browser. Nothing to copy."))
    .child(div().pt(6).child(btn("connect-moshi", t, "Connect", connect, { primary: true })));
}
