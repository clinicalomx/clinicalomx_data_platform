/* ROI canvas: a bidirectional Streamlit component (fabric.js) with a RIGHT-CLICK context menu
 * to change a box's class, plus move/resize/add/delete + clamp-to-image. Replaces
 * streamlit-drawable-canvas for the review app, which can't do per-object right-click.
 *
 * Protocol (hand-rolled, no streamlit-component-lib build step):
 *   component -> host: {isStreamlitMessage:true, type:"streamlit:componentReady"|"setFrameHeight"|"setComponentValue", ...}
 *   host -> component : window 'message' with data.type === "streamlit:render", data.args = python kwargs
 *
 * Args (from python roi_canvas()): image_b64, boxes[{class,xyxy,occupied,rc,bid}], classes[], colors{cls:hex},
 *   width, height (NATURAL review px), max_display_w, edges[{a,b,axis}], nonce. We rebuild the canvas
 *   ONLY when `nonce` changes (python bumps it to reseed, e.g. after Dearray). Otherwise we keep the
 *   live edit state and just emit values, exactly like st_canvas's key/initial_drawing.
 * Value (to python): {boxes:[{class,xyxy,occupied,rc,bid}], edges:[{a,b,axis}], nonce}. xyxy is
 *   NATURAL px, clamped to the image.
 *
 * EDGES. Two kinds, both drawn behind the boxes:
 *   - the MESH: the grid's own connectivity, one thin line per rc-adjacent pair. Derived HERE from
 *     `rc` (not passed in) so it stays correct as boxes are moved or deleted between Streamlit
 *     reruns. Python only re-seeds on a nonce bump, so a server-computed mesh would go stale.
 *   - LINKS: reviewer priors ("these two cores are the same row"). Thick, white-haloed, keyed by the
 *     stable `bid`, and the only edges sent back to python. review.grid_links.apply_grid_links turns
 *     them into a corrected cell indexing.
 * Axis colours match tma_gridding.plot_grid's lattice layer: orange = column-step (same row),
 * deepskyblue = row-step (same column), so the app and the matplotlib QA plots read alike.
 */
(function () {
  "use strict";

  // ---- Streamlit handshake -------------------------------------------------
  function send(type, extra) {
    window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: "streamlit:" + type }, extra || {}), "*");
  }
  function setFrameHeight(h) { send("setFrameHeight", { height: h }); }
  function setValue(value) { send("setComponentValue", { value: value, dataType: "json" }); }

  var fabricCanvas = null;
  var state = { nonce: null, classes: [], colors: {}, defaultClass: null,
                naturalW: 0, naturalH: 0, scale: 1,
                links: [],      // reviewer priors: [{a: bid, b: bid, axis: "row"|"col"}]
                nextBid: 0 };   // for boxes drawn on the canvas, which arrive without a bid

  var AXIS_COLOR = { row: "#ff9f0a", col: "#0ac8ff" };  // orange = same row, cyan = same column

  var ctx = document.getElementById("ctx");
  var wrap = document.getElementById("wrap");
  var btnMove = document.getElementById("mode-move");
  var btnAdd = document.getElementById("mode-add");
  var btnPan = document.getElementById("mode-pan");
  var btnLink = document.getElementById("mode-link");
  var chkMesh = document.getElementById("show-mesh");
  var zoomLabel = document.getElementById("zoom-label");
  var fBright = document.getElementById("f-bright");
  var fContrast = document.getElementById("f-contrast");
  var fGamma = document.getElementById("f-gamma");
  var bgImg = null;  // the background fabric.Image (for brightness/contrast/gamma filters)

  // ---- helpers -------------------------------------------------------------
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function isBox(o) { return o && o.roiBox === true; }

  function color(cls) { return state.colors[cls] || "#ff3b30"; }

  // ---- image adjustments (help with dim / low-contrast slides) -------------
  // Brightness/Contrast in [-1,1] (0 = neutral); Gamma per-channel (1 = neutral, <1 brightens
  // shadows). Applied as fabric image filters on the background; re-applied after each rebuild so
  // the settings survive a Dearray reseed. Slider DOM lives in the toolbar (outside the canvas).
  function applyFilters() {
    if (!bgImg || !fabricCanvas) return;
    var b = +fBright.value, c = +fContrast.value, g = +fGamma.value;
    bgImg.filters = [
      new fabric.Image.filters.Brightness({ brightness: b }),
      new fabric.Image.filters.Contrast({ contrast: c }),
      new fabric.Image.filters.Gamma({ gamma: [g, g, g] }),
    ];
    try { bgImg.applyFilters(); } catch (e) { /* filter backend unavailable */ }
    fabricCanvas.requestRenderAll();
  }

  // ---- zoom / pan (viewport transform only, never touches box coordinates) --
  function updateZoomLabel() {
    if (fabricCanvas && zoomLabel) zoomLabel.textContent = Math.round(fabricCanvas.getZoom() * 100) + "%";
  }
  function zoomTo(z, pt) {
    if (!fabricCanvas) return;
    z = clamp(z, 0.2, 8);
    if (pt) fabricCanvas.zoomToPoint(pt, z); else fabricCanvas.setZoom(z);
    updateZoomLabel();
  }
  function centerPt() { return { x: fabricCanvas.getWidth() / 2, y: fabricCanvas.getHeight() / 2 }; }
  function resetView() { if (fabricCanvas) { fabricCanvas.setViewportTransform([1, 0, 0, 1, 0, 0]); updateZoomLabel(); } }

  // Bake a rect's scale into its width/height and clamp it inside the canvas (display px).
  function normalizeRect(r) {
    var w = r.width * r.scaleX, h = r.height * r.scaleY;
    var left = r.left, top = r.top;
    // fabric origin is top-left here; keep it that way
    left = clamp(left, 0, state.naturalW / state.scale);
    top = clamp(top, 0, state.naturalH / state.scale);
    w = clamp(w, 1, state.naturalW / state.scale - left);
    h = clamp(h, 1, state.naturalH / state.scale - top);
    r.set({ left: left, top: top, width: w, height: h, scaleX: 1, scaleY: 1 });
    r.setCoords();
  }

  function styleRect(r) {
    r.set({
      stroke: color(r.cls),
      strokeDashArray: r.occupied ? null : [6, 4],  // dashed = grid-inferred
      strokeUniform: true,
    });
  }

  function labelText(r) {
    if (r.rc) return r.rc[0] + "," + r.rc[1];
    return r.cls;
  }

  function labelPos(r) { return { left: r.left + 2, top: Math.max(0, r.top - 15) }; }

  // Labels are separate, non-interactive Text objects. Rebuilt on structural changes; during a
  // drag/resize we only move the ONE label of the box being edited (see moveLabel), so it tracks
  // the box live without the O(n) remove+re-add churn.
  function redrawLabels() {
    if (!fabricCanvas) return;
    fabricCanvas.getObjects().filter(function (o) { return o.roiLabel; })
      .forEach(function (o) { fabricCanvas.remove(o); });
    fabricCanvas.getObjects().filter(isBox).forEach(function (r) {
      var p = labelPos(r);
      var t = new fabric.Text(labelText(r), {
        left: p.left, top: p.top,
        fontSize: 13, fontFamily: "monospace",
        fill: color(r.cls), backgroundColor: "rgba(0,0,0,0.35)",
        selectable: false, evented: false, roiLabel: true,
      });
      r._label = t;         // link so moveLabel can follow this box during drag/resize
      fabricCanvas.add(t);
    });
    fabricCanvas.requestRenderAll();
  }

  // ---- edges: the derived grid mesh + the reviewer's links --------------------
  function isEdge(o) { return o && o.roiEdge === true; }
  function boxes() { return fabricCanvas ? fabricCanvas.getObjects().filter(isBox) : []; }
  function boxByBid(bid) {
    var hit = boxes().filter(function (r) { return r.bid === bid; });
    return hit.length ? hit[0] : null;
  }
  function centreOf(r) {
    return { x: r.left + (r.width * r.scaleX) / 2, y: r.top + (r.height * r.scaleY) / 2 };
  }

  // Which axis does a link between two boxes assert? A same-row pair is separated mostly in x,
  // a same-column pair mostly in y. Inferred on creation; right-click the link to flip it, because
  // a badly shifted core, exactly the case links exist for, can fool the guess.
  function guessAxis(a, b) {
    var ca = centreOf(a), cb = centreOf(b);
    return Math.abs(cb.x - ca.x) >= Math.abs(cb.y - ca.y) ? "row" : "col";
  }

  function makeLine(a, b, opts) {
    var ca = centreOf(a), cb = centreOf(b);
    var ln = new fabric.Line([ca.x, ca.y, cb.x, cb.y], Object.assign({
      selectable: false, evented: false, objectCaching: false, strokeUniform: true,
    }, opts));
    ln.roiEdge = true; ln.eA = a.bid; ln.eB = b.bid;
    return ln;
  }

  // The mesh: one thin line per rc-adjacent pair, recomputed from `rc` every redraw.
  function meshLines() {
    var byRc = {}, out = [];
    boxes().forEach(function (r) { if (r.rc) byRc[r.rc[0] + "," + r.rc[1]] = r; });
    Object.keys(byRc).forEach(function (k) {
      var r = byRc[k], i = r.rc[0], j = r.rc[1];
      var right = byRc[i + "," + (j + 1)], below = byRc[(i + 1) + "," + j];
      if (right) out.push(makeLine(r, right, { stroke: AXIS_COLOR.row, strokeWidth: 1, opacity: 0.55 }));
      if (below) out.push(makeLine(r, below, { stroke: AXIS_COLOR.col, strokeWidth: 1, opacity: 0.55 }));
    });
    return out;
  }

  // A link: a white halo under a thick coloured line, so a prior is unmistakable against the mesh.
  // Only the top line is evented (right-click -> flip / delete); both carry the bids so a drag can
  // move them together.
  function linkLines(lk) {
    var a = boxByBid(lk.a), b = boxByBid(lk.b);
    if (!a || !b) return [];  // endpoint was deleted; the link is dropped on the next emit
    var halo = makeLine(a, b, { stroke: "#ffffff", strokeWidth: 6, opacity: 0.9 });
    var core = makeLine(a, b, { stroke: AXIS_COLOR[lk.axis] || AXIS_COLOR.row, strokeWidth: 3 });
    core.evented = true; core.link = lk; core.perPixelTargetFind = true;
    return [halo, core];
  }

  function redrawEdges() {
    if (!fabricCanvas) return;
    fabricCanvas.getObjects().filter(isEdge).forEach(function (o) { fabricCanvas.remove(o); });
    var add = [];
    if (!chkMesh || chkMesh.checked) add = add.concat(meshLines());
    state.links.forEach(function (lk) { add = add.concat(linkLines(lk)); });
    add.forEach(function (ln) { fabricCanvas.add(ln); fabricCanvas.sendToBack(ln); });
    fabricCanvas.requestRenderAll();
  }

  // Live-follow: reposition only the edited box's label and the edges touching it, as it
  // moves/resizes. Same reason as redrawLabels: a full-TMA remove+re-add per mouse event janks.
  function moveLabel(e) {
    var r = e.target;
    if (!isBox(r)) return;
    if (r._label) { var p = labelPos(r); r._label.set(p); r._label.setCoords(); }
    var c = centreOf(r);
    fabricCanvas.getObjects().filter(isEdge).forEach(function (ln) {
      if (ln.eA === r.bid) ln.set({ x1: c.x, y1: c.y });
      else if (ln.eB === r.bid) ln.set({ x2: c.x, y2: c.y });
    });
    fabricCanvas.requestRenderAll();
  }

  // Drop links whose endpoint no longer exists (box deleted), so python never sees a dangling bid.
  function pruneLinks() {
    var live = {};
    boxes().forEach(function (r) { live[r.bid] = true; });
    var before = state.links.length;
    state.links = state.links.filter(function (lk) { return live[lk.a] && live[lk.b]; });
    return state.links.length !== before;
  }

  // ---- emit current boxes back to python -----------------------------------
  function emit() {
    if (!fabricCanvas) return;
    pruneLinks();
    var s = state.scale;
    var out = boxes().map(function (r) {
      var x1 = r.left * s, y1 = r.top * s;
      var x2 = (r.left + r.width * r.scaleX) * s, y2 = (r.top + r.height * r.scaleY) * s;
      x1 = clamp(x1, 0, state.naturalW); x2 = clamp(x2, 0, state.naturalW);
      y1 = clamp(y1, 0, state.naturalH); y2 = clamp(y2, 0, state.naturalH);
      return {
        "class": r.cls,
        xyxy: [Math.round(x1 * 10) / 10, Math.round(y1 * 10) / 10,
               Math.round(x2 * 10) / 10, Math.round(y2 * 10) / 10],
        occupied: !!r.occupied,
        rc: r.rc || null,
        bid: r.bid,
      };
    });
    setValue({ boxes: out, edges: state.links, nonce: state.nonce });
  }

  // ---- right-click context menu (class list + delete) ----------------------
  function hideCtx() { ctx.style.display = "none"; }
  function showCtx(target, x, y) {
    ctx.innerHTML = "";
    var hdr = document.createElement("div"); hdr.className = "hdr"; hdr.textContent = "set class";
    ctx.appendChild(hdr);
    state.classes.forEach(function (cls) {
      var item = document.createElement("div"); item.className = "item";
      var sw = document.createElement("span"); sw.className = "sw"; sw.style.background = color(cls);
      var lbl = document.createElement("span"); lbl.textContent = cls + (cls === target.cls ? "  ✓" : "");
      item.appendChild(sw); item.appendChild(lbl);
      item.onclick = function () {
        target.cls = cls; styleRect(target);
        redrawLabels(); fabricCanvas.requestRenderAll(); hideCtx(); emit();
      };
      ctx.appendChild(item);
    });
    var sep = document.createElement("div"); sep.className = "sep"; ctx.appendChild(sep);
    var del = document.createElement("div"); del.className = "item del"; del.textContent = "✕ delete box";
    del.onclick = function () {
      fabricCanvas.remove(target); redrawLabels(); redrawEdges();
      fabricCanvas.requestRenderAll(); hideCtx(); emit();
    };
    ctx.appendChild(del);
    ctx.style.left = x + "px"; ctx.style.top = y + "px"; ctx.style.display = "block";
  }
  // Right-click a LINK: flip which axis it asserts, or remove it. The flip matters because the
  // axis is guessed from geometry, and the misaligned cores that links exist for are exactly where
  // that guess can be wrong.
  function showLinkCtx(line, x, y) {
    ctx.innerHTML = "";
    var lk = line.link;
    var hdr = document.createElement("div"); hdr.className = "hdr"; hdr.textContent = "grid link";
    ctx.appendChild(hdr);
    [["row", "same row (adjacent columns)"], ["col", "same column (adjacent rows)"]]
      .forEach(function (pair) {
        var item = document.createElement("div"); item.className = "item";
        var sw = document.createElement("span"); sw.className = "sw";
        sw.style.background = AXIS_COLOR[pair[0]];
        var lbl = document.createElement("span");
        lbl.textContent = pair[1] + (lk.axis === pair[0] ? "  ✓" : "");
        item.appendChild(sw); item.appendChild(lbl);
        item.onclick = function () { lk.axis = pair[0]; redrawEdges(); hideCtx(); emit(); };
        ctx.appendChild(item);
      });
    var sep = document.createElement("div"); sep.className = "sep"; ctx.appendChild(sep);
    var del = document.createElement("div"); del.className = "item del"; del.textContent = "✕ remove link";
    del.onclick = function () {
      state.links = state.links.filter(function (o) { return o !== lk; });
      redrawEdges(); hideCtx(); emit();
    };
    ctx.appendChild(del);
    ctx.style.left = x + "px"; ctx.style.top = y + "px"; ctx.style.display = "block";
  }
  document.addEventListener("click", function (e) { if (!ctx.contains(e.target)) hideCtx(); });

  // ---- draw (add) mode + pan state -----------------------------------------
  var mode = "move", drawing = null, drawStart = null;
  var panning = false, lastPan = null;
  var linkPick = null;  // first box of a link being made (null = nothing picked yet)

  // Show which box is half-way through a link. Restored by styleRect on the next redraw.
  function highlightPick(r, on) {
    if (!r) return;
    if (on) r.set({ stroke: "#ffffff", strokeWidth: 4 });
    else { r.set({ strokeWidth: 2 }); styleRect(r); }
    fabricCanvas.requestRenderAll();
  }
  function cancelPick() {
    if (linkPick) { highlightPick(linkPick, false); linkPick = null; }
  }

  function setMode(m) {
    mode = m;
    cancelPick();
    btnMove.classList.toggle("active", m === "move");
    btnAdd.classList.toggle("active", m === "add");
    if (btnPan) btnPan.classList.toggle("active", m === "pan");
    if (btnLink) btnLink.classList.toggle("active", m === "link");
    if (fabricCanvas) {
      fabricCanvas.selection = m === "move";
      // In pan mode boxes are non-interactive so a drag always pans (no accidental box move). In
      // link mode they stay evented (we need the clicks) but not selectable/draggable.
      fabricCanvas.getObjects().filter(isBox).forEach(function (r) {
        r.selectable = m === "move"; r.evented = m !== "pan";
      });
      fabricCanvas.defaultCursor = (m === "add" || m === "link") ? "crosshair"
                                 : (m === "pan" ? "grab" : "default");
      fabricCanvas.requestRenderAll();
    }
    hideCtx();
  }
  btnMove.onclick = function () { setMode("move"); };
  btnAdd.onclick = function () { setMode("add"); };
  if (btnPan) btnPan.onclick = function () { setMode("pan"); };
  if (btnLink) btnLink.onclick = function () { setMode("link"); };
  if (chkMesh) chkMesh.onchange = redrawEdges;

  // Click box A then box B to assert they are the same row (or column).
  function pickForLink(target) {
    if (!linkPick) { linkPick = target; highlightPick(target, true); return; }
    if (linkPick === target) { cancelPick(); return; }  // clicking the same box cancels
    var a = linkPick;
    highlightPick(a, false);
    linkPick = null;
    var dup = state.links.some(function (lk) {
      return (lk.a === a.bid && lk.b === target.bid) || (lk.a === target.bid && lk.b === a.bid);
    });
    if (!dup) state.links.push({ a: a.bid, b: target.bid, axis: guessAxis(a, target) });
    redrawEdges();
    emit();
  }

  // ---- (re)build the canvas from args --------------------------------------
  function build(args) {
    state.nonce = args.nonce;
    state.classes = args.classes || [];
    state.colors = args.colors || {};
    state.defaultClass = state.classes[0] || "full_core";
    state.naturalW = args.width; state.naturalH = args.height;
    var maxW = args.max_display_w || 900;
    state.scale = state.naturalW > maxW ? state.naturalW / maxW : 1;
    var dispW = Math.round(state.naturalW / state.scale);
    var dispH = Math.round(state.naturalH / state.scale);

    if (fabricCanvas) { try { fabricCanvas.dispose(); } catch (e) { /* ignore */ } }
    // Recreate a fresh <canvas> element: fabric's dispose()+reuse of the same node is fragile.
    var holder = document.getElementById("canvas-holder");
    holder.innerHTML = '<canvas id="c"></canvas>';
    fabricCanvas = new fabric.Canvas(document.getElementById("c"), {
      width: dispW, height: dispH, selection: true,
      uniformScaling: false, preserveObjectStacking: true,
      // fabric ignores right/middle clicks unless told to fire them; stopContextMenu suppresses
      // the browser menu so our own context menu shows instead. fireMiddleClick enables
      // middle-mouse-drag panning.
      fireRightClick: true, fireMiddleClick: true, stopContextMenu: true });

    // prevent the browser menu; fabric 'mouse:down' with button 3 drives our menu instead
    fabricCanvas.upperCanvasEl.addEventListener("contextmenu", function (e) { e.preventDefault(); });

    // background image (data URI). Keep a ref so the brightness/contrast/gamma filters can be
    // (re)applied; re-apply current slider values after load so they survive a rebuild/reseed.
    bgImg = null;
    if (args.image_b64) {
      fabric.Image.fromURL("data:image/png;base64," + args.image_b64, function (img) {
        img.set({ left: 0, top: 0, selectable: false, evented: false });
        img.scaleToWidth(dispW);
        bgImg = img;
        fabricCanvas.setBackgroundImage(img, function () { applyFilters(); fabricCanvas.requestRenderAll(); });
      });
    }

    // boxes
    state.nextBid = 0;
    (args.boxes || []).forEach(function (b) {
      var xy = b.xyxy, s = state.scale;
      var r = new fabric.Rect({
        left: xy[0] / s, top: xy[1] / s,
        width: (xy[2] - xy[0]) / s, height: (xy[3] - xy[1]) / s,
        fill: "rgba(0,0,0,0)", strokeWidth: 2, cornerSize: 7, transparentCorners: false,
        objectCaching: false,
      });
      r.roiBox = true;
      r.cls = b["class"];
      r.occupied = b.occupied !== false;
      r.rc = b.rc || null;
      // Stable identity for links. Python assigns bids; a box seeded without one (older payload)
      // gets a local id so links can still reference it.
      r.bid = (b.bid === undefined || b.bid === null) ? state.nextBid++ : b.bid;
      state.nextBid = Math.max(state.nextBid, r.bid + 1);
      styleRect(r);
      fabricCanvas.add(r);
    });

    // Links come from python (session state), so they survive a Dearray reseed. Drop any whose
    // endpoints didn't come back in this seed.
    state.links = (args.edges || []).map(function (e) {
      return { a: e.a, b: e.b, axis: e.axis === "col" ? "col" : "row" };
    });
    pruneLinks();

    wireEvents();
    setMode("move");
    redrawLabels();
    redrawEdges();
    resetView();
    fabricCanvas.requestRenderAll();
    // measure the two toolbars so the iframe is tall enough for canvas + controls
    var tb = (document.getElementById("toolbar").offsetHeight || 34)
           + (document.getElementById("toolbar2").offsetHeight || 34);
    setFrameHeight(dispH + tb + 18);
    emit();  // seed python with the initial boxes
  }

  function wireEvents() {
    // Emit + reposition labels on mouseup (object:modified). We deliberately DON'T redraw labels
    // on every object:moving/scaling tick: that removes+re-adds a Text per box per mouse event
    // and janks with a full TMA of cores. Labels lag until mouseup, which is fine.
    fabricCanvas.on("object:modified", function (e) {
      if (isBox(e.target)) { normalizeRect(e.target); redrawLabels(); redrawEdges(); emit(); }
    });
    // label tracks its box live during a drag/resize (only the edited box's label moves)
    fabricCanvas.on("object:moving", moveLabel);
    fabricCanvas.on("object:scaling", moveLabel);

    // wheel = zoom toward the cursor
    fabricCanvas.on("mouse:wheel", function (opt) {
      var z = fabricCanvas.getZoom() * Math.pow(0.999, opt.e.deltaY);
      zoomTo(z, { x: opt.e.offsetX, y: opt.e.offsetY });
      opt.e.preventDefault(); opt.e.stopPropagation();
    });

    // right-click -> context menu on the box under the cursor. fabric reports button===3 for the
    // right button (with fireRightClick on); also accept the native DOM button===2 as a fallback.
    fabricCanvas.on("mouse:down", function (opt) {
      // Pan: dedicated Pan mode (boxes non-interactive), or middle-mouse-drag from any mode.
      // fabric reports the middle button as button===2 (with fireMiddleClick on); accept the
      // native DOM button===1 too.
      var isMiddle = opt.button === 2 || (opt.e && opt.e.button === 1);
      if (mode === "pan" || isMiddle) {
        panning = true; fabricCanvas.selection = false;
        fabricCanvas.discardActiveObject();  // don't drag a box while panning
        lastPan = { x: opt.e.clientX, y: opt.e.clientY };
        fabricCanvas.setCursor("grabbing");
        if (opt.e && opt.e.preventDefault) opt.e.preventDefault();  // stop middle-click autoscroll
        return;
      }
      var isRight = opt.button === 3 || (opt.e && opt.e.button === 2);
      if (isRight && isBox(opt.target)) {
        showCtx(opt.target, opt.e.offsetX, opt.e.offsetY);
        if (opt.e.preventDefault) opt.e.preventDefault();
        return;
      }
      if (isRight && isEdge(opt.target) && opt.target.link) {
        showLinkCtx(opt.target, opt.e.offsetX, opt.e.offsetY);
        if (opt.e.preventDefault) opt.e.preventDefault();
        return;
      }
      if (isRight) { return; }  // right-click on empty canvas: do nothing (don't start a draw)
      hideCtx();
      if (mode === "link") {
        if (isBox(opt.target)) pickForLink(opt.target);
        else cancelPick();  // click on empty canvas abandons a half-made link
        return;
      }
      if (mode === "add" && !opt.target) {
        var pt = fabricCanvas.getPointer(opt.e);
        drawStart = pt;
        drawing = new fabric.Rect({
          left: pt.x, top: pt.y, width: 1, height: 1,
          fill: "rgba(0,0,0,0)", stroke: color(state.defaultClass), strokeWidth: 2,
          cornerSize: 7, transparentCorners: false, objectCaching: false,
        });
        drawing.roiBox = true; drawing.cls = state.defaultClass; drawing.occupied = true; drawing.rc = null;
        drawing.bid = state.nextBid++;  // linkable as soon as it exists
        fabricCanvas.add(drawing);
      }
    });
    fabricCanvas.on("mouse:move", function (opt) {
      if (panning) {
        var vpt = fabricCanvas.viewportTransform;
        vpt[4] += opt.e.clientX - lastPan.x;
        vpt[5] += opt.e.clientY - lastPan.y;
        lastPan = { x: opt.e.clientX, y: opt.e.clientY };
        fabricCanvas.requestRenderAll();
        return;
      }
      if (mode === "add" && drawing) {
        var pt = fabricCanvas.getPointer(opt.e);
        drawing.set({ width: Math.abs(pt.x - drawStart.x), height: Math.abs(pt.y - drawStart.y),
          left: Math.min(pt.x, drawStart.x), top: Math.min(pt.y, drawStart.y) });
        fabricCanvas.requestRenderAll();
      }
    });
    fabricCanvas.on("mouse:up", function () {
      if (panning) {
        panning = false; lastPan = null;
        fabricCanvas.selection = mode === "move";
        fabricCanvas.setViewportTransform(fabricCanvas.viewportTransform);  // commit
        return;
      }
      if (mode === "add" && drawing) {
        if (drawing.width < 4 || drawing.height < 4) { fabricCanvas.remove(drawing); }
        else { normalizeRect(drawing); styleRect(drawing); redrawLabels(); emit(); }
        drawing = null; drawStart = null;
      }
    });
  }

  // delete selected box with Delete/Backspace; Esc abandons a half-made link
  document.addEventListener("keydown", function (e) {
    if ((e.key === "Delete" || e.key === "Backspace") && fabricCanvas) {
      var a = fabricCanvas.getActiveObject();
      if (isBox(a)) {
        fabricCanvas.remove(a); redrawLabels(); redrawEdges();
        fabricCanvas.requestRenderAll(); emit();
      }
    }
    if (e.key === "Escape") { cancelPick(); hideCtx(); }
  });

  // ---- toolbar-2 controls (wired once; operate on the current canvas) -------
  document.getElementById("zoom-in").onclick = function () { if (fabricCanvas) zoomTo(fabricCanvas.getZoom() * 1.25, centerPt()); };
  document.getElementById("zoom-out").onclick = function () { if (fabricCanvas) zoomTo(fabricCanvas.getZoom() / 1.25, centerPt()); };
  document.getElementById("zoom-reset").onclick = resetView;
  [fBright, fContrast, fGamma].forEach(function (el) { el.addEventListener("input", applyFilters); });
  document.getElementById("f-reset").onclick = function () {
    fBright.value = 0; fContrast.value = 0; fGamma.value = 1; applyFilters();
  };

  // ---- receive render events from Streamlit --------------------------------
  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.type !== "streamlit:render") return;
    var args = data.args || {};
    if (args.nonce === state.nonce && fabricCanvas) return;  // same seed -> keep live edits
    build(args);
  });

  send("componentReady", { apiVersion: 1 });
})();
