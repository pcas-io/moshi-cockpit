// The first screen on a new Mac: one sentence, one button. The tokens are asked for by
// `moshi-cockpit connect` in a terminal tab; this mod never sees them.

import { div } from "gpui-kit";
import { v_flex } from "gpui-base";
import { home, run, props, closeOverlay } from "herdr";
import { btn } from "./util.js";

function connect() {
  run("tab.new", { name: "Mit moshi verbinden", argv: [`${home()}/.local/bin/moshi-cockpit`, "connect"] });
  closeOverlay(props().overlay);
}

export function connectKey(e) {
  if (e.key === "enter") connect();
}

export function connectView(t) {
  return v_flex().size_full().bg(t.popover).p(24).gap(12).items_center().justify_center()
    .child(div().font_semibold().text_size(16).text_color(t.foreground).child("Willkommen in deinem Cockpit"))
    .child(div().text_size(13).text_color(t.dim).text_center().child("Verbinde diesen Mac, damit deine Agents zusammenarbeiten können."))
    .child(div().pt(6).child(btn("connect-moshi", t, "Mit moshi verbinden", connect, { primary: true })));
}
