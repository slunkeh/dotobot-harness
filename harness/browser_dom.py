"""Bounded HTML browser actions on the bot's existing, profile-verified CDP session.

The fixed script runs in an isolated world. Models only supply operation names,
observed indices and literal field text; they never supply executable code.
"""

from __future__ import annotations

from . import cdp

# ponytail: ordinary top-level HTML only; use visual computer tools for frames,
# shadow DOM, canvas, rich editors, uploads and custom keyboard widgets.
_SCRIPT = r"""(() => {
    let previous = null;
    let outgoing = null;
    const visible = e => {
        if (e.checkVisibility && !e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return false;
        const r = e.getBoundingClientRect(), s = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
            r.top < innerHeight && r.left < innerWidth &&
            s.display !== 'none' && s.visibility === 'visible' && s.opacity !== '0';
    };
    const sensitive = e => e.matches('input[type=password],input[type=file]') ||
        /(?:password|cc-|one-time-code)/i.test(e.autocomplete || '');
    const name = e => (e.getAttribute('aria-label') ||
        (e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id =>
            document.getElementById(id)?.textContent || '').join(' ').trim() ||
        Array.from(e.labels || []).map(l => l.innerText).join(' ') ||
        e.innerText || e.getAttribute('placeholder') || e.title || '').trim().slice(0, 240);
    function read() {
        const nodes = [], elements = [];
        let unsupported = false, scanned = 0;
        for (const e of document.querySelectorAll('*')) {
            if (++scanned > 12000) { unsupported = true; break; }
            if (!visible(e)) continue;
            if (e.matches('iframe,canvas') || e.isContentEditable || e.shadowRoot)
                unsupported = true;
            if (!e.matches('a[href],button,input,textarea,select,[role=button],[role=link],[role=checkbox],[role=tab]') ||
                sensitive(e) || e.matches(':disabled') || e.closest('[inert]') ||
                e.getAttribute('aria-disabled') === 'true') continue;
            let operations = ['CLICK'];
            if (e.matches('input,textarea')) {
                if (e.matches('input') && !['text','search','email','url','tel','number','button','submit','reset','checkbox','radio'].includes(e.type)) continue;
                if (e.matches('textarea,input[type=text],input[type=search],input[type=email],input[type=url],input[type=tel],input[type=number],input:not([type])'))
                    operations = e.readOnly ? [] : ['TYPE_TEXT'];
            }
            if (e.matches('select')) operations = e.multiple ? [] : ['SELECT'];
            if (!operations.length) continue;
            if (nodes.length >= 100) { unsupported = true; break; }
            nodes.push(e);
            const item = {id: String(nodes.length), role: e.getAttribute('role') || e.tagName.toLowerCase(),
                label: name(e), value: String(e.value || '').slice(0, 500),
                checked: !!e.checked, expanded: e.getAttribute('aria-expanded'),
                destination: e.href || e.formAction || e.form?.action || '',
                form_method: e.formMethod || e.form?.method || '', operations};
            if (operations.includes('SELECT')) {
                if (e.options.length > 80) { unsupported = true; break; }
                item.options = Array.from(e.options).map((o, i) => ({id: String(i), label: o.label.slice(0, 240),
                    disabled: o.disabled || !!o.closest('optgroup[disabled]')})).filter(o => !o.disabled);
            }
            elements.push(item);
        }
        let text = '', count = 0;
        const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
        while (walker.nextNode() && text.length < 6000 && count++ < 12000) {
            const e = walker.currentNode.parentElement;
            if (!e || e.closest('script,style,noscript,input,textarea,select,[hidden],[aria-hidden=true]') || !visible(e)) continue;
            const r = document.createRange(); r.selectNodeContents(walker.currentNode);
            if (!Array.from(r.getClientRects()).some(b => b.bottom > 0 && b.top < innerHeight && b.right > 0 && b.left < innerWidth)) continue;
            const value = walker.currentNode.textContent.trim();
            if (value) text += value + '\n';
        }
        const data = {url: location.href, title: document.title.slice(0, 240), text: text.slice(0, 6000),
            visibility: document.visibilityState,
            elements, unsupported, scroll_up: scrollY > 0,
            scroll_down: scrollY + innerHeight < document.documentElement.scrollHeight - 2};
        // Full field values and option identities stay local, including text past the model's cap.
        const signature = JSON.stringify([data, scrollX, scrollY, nodes.map(e => [e.value,
            Array.from(e.options || []).map(o => [o.value, o.label, o.disabled])]),
            Array.from(document.querySelectorAll('input,select,textarea')).map(e =>
                [e.type, e.name, e.value, e.checked, e.disabled, e.form?.action, e.form?.method])]);
        return {data, nodes, signature};
    }
    function messageForm(url, text) {
        if (location.href !== url || document.visibilityState !== 'visible' ||
            typeof text !== 'string' || !text.trim() || text.length > 2000) return null;
        const fields = Array.from(document.querySelectorAll('textarea')).filter(e =>
            visible(e) && !e.disabled && !e.readOnly && !e.closest('[inert]'));
        if (fields.length !== 1) return null;
        const field = fields[0], form = field.form;
        if (!form || new URL(form.action, location.href).origin !== location.origin ||
            (field.value !== '' && field.value !== text)) return null;
        const buttons = Array.from(form.elements).filter(e =>
            ['BUTTON','INPUT'].includes(e.tagName) && e.type === 'submit' && visible(e) && !e.disabled);
        if (buttons.length !== 1) return null;
        const button = buttons[0], r = button.getBoundingClientRect();
        const top = document.elementFromPoint((Math.max(0,r.left)+Math.min(innerWidth,r.right))/2,
            (Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2);
        if (!top || !(top === button || button.contains(top))) return null;
        const controls = Array.from(form.elements);
        // Input handlers may rewrite hidden recipients or the submit endpoint.
        // Permit only the approved textarea value to change during preparation.
        const binding = JSON.stringify([form.action, form.method, form.target,
            button.formAction, button.formMethod, button.formTarget,
            controls.map(e => [e.tagName, e.type, e.name, e === field ? null : e.value,
                e.checked, e.disabled])]);
        if (new URL(button.formAction || form.action, location.href).origin !== location.origin) return null;
        return {field, form, button, controls, binding, signature: read().signature};
    }
    return {
        prepareOutgoing(url, text) {
            outgoing = messageForm(url, text);
            return outgoing ? 'ready' : 'unsupported';
        },
        submitOutgoing(url, text) {
            const old = outgoing; outgoing = null;
            const current = messageForm(url, text);
            if (!old || !current || old.field !== current.field || old.button !== current.button ||
                old.form !== current.form || old.signature !== current.signature) return 'stale';
            if (current.field.value === '') {
                Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(current.field, text);
                current.field.dispatchEvent(new Event('input', {bubbles:true}));
                current.field.dispatchEvent(new Event('change', {bubbles:true}));
            }
            const checked = messageForm(url, text);
            if (!checked || checked.field !== current.field || checked.button !== current.button ||
                checked.form !== current.form || checked.field.value !== text ||
                checked.binding !== current.binding || checked.controls.length !== current.controls.length ||
                checked.controls.some((e, i) => e !== current.controls[i])) return 'stale';
            checked.button.click();
            return 'ok';
        },
        observe() { previous = read(); return previous.data; },
        act(operation, target, text) {
            if (!previous) return 'stale';
            const old = previous; previous = null; // consume before any side effect
            const current = read();
            if (current.data.visibility !== 'visible' || old.signature !== current.signature || old.nodes.length !== current.nodes.length ||
                old.nodes.some((e, i) => e !== current.nodes[i] || !e.isConnected)) return 'stale';
            if (operation === 'WAIT') return 'ok';
            if (operation === 'SCROLL_UP' || operation === 'SCROLL_DOWN') {
                window.scrollBy(0, (operation === 'SCROLL_UP' ? -1 : 1) * innerHeight * 0.7);
                return 'ok';
            }
            const [id, option] = String(target).split(':');
            const item = old.data.elements.find(e => e.id === id);
            if (!item || !item.operations.includes(operation)) return 'unsupported';
            const e = old.nodes[Number(id) - 1], r = e.getBoundingClientRect();
            const x = (Math.max(0, r.left) + Math.min(innerWidth, r.right)) / 2;
            const y = (Math.max(0, r.top) + Math.min(innerHeight, r.bottom)) / 2;
            const top = document.elementFromPoint(x, y);
            if (!top || !(top === e || e.contains(top)) || sensitive(e) || e.matches(':disabled')) return 'stale';
            if (operation === 'CLICK') { e.click(); return 'ok'; }
            if (operation === 'TYPE_TEXT') {
                if (typeof text !== 'string' || !text.trim() || text.length > 2000 || e.readOnly) return 'unsupported';
                const prototype = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                Object.getOwnPropertyDescriptor(prototype, 'value').set.call(e, text);
                e.dispatchEvent(new Event('input', {bubbles: true}));
                e.dispatchEvent(new Event('change', {bubbles: true}));
                return 'ok';
            }
            if (operation === 'SELECT' && item.options.some(o => o.id === option)) {
                e.selectedIndex = Number(option);
                e.dispatchEvent(new Event('input', {bubbles: true}));
                e.dispatchEvent(new Event('change', {bubbles: true}));
                return 'ok';
            }
            return 'unsupported';
        }
    };
})()"""


class Browser:
    def __init__(self, *, machine=None, user_data_dir=None, guard=lambda: None):
        self.machine, self.profile, self.guard = machine, user_data_dir, guard
        self.session = None

    def __enter__(self):
        self.guard()
        if not cdp.enabled():
            raise cdp.CdpError("Browser access is disabled")
        port = cdp._discover_port(self.machine, self.profile)
        if port is None:
            raise cdp.CdpError("Reopen Chrome with browser access enabled")
        tabs = cdp._http_json(self.machine, port, "/json/list")
        path = _visible_page_path(self.machine, port, tabs)
        try:
            self.session = cdp._Session(self.machine, port, path)
            self.session.__enter__()
            self._initialize()
        except Exception:
            self.__exit__()
            raise
        return self

    def _initialize(self):
        tree = self.session.call("Page.getFrameTree") or {}
        frame = tree["frameTree"]["frame"]["id"]
        world = (
            self.session.call(
                "Page.createIsolatedWorld", frameId=frame, worldName="dotobot-browser"
            )
            or {}
        )
        result = (
            self.session.call(
                "Runtime.evaluate", expression=_SCRIPT, contextId=world["executionContextId"]
            )
            or {}
        )
        if result.get("exceptionDetails"):
            raise cdp.CdpError("Browser observation failed")
        self.object_id = result["result"]["objectId"]

    def _call(self, function, *args):
        result = (
            self.session.call(
                "Runtime.callFunctionOn",
                objectId=self.object_id,
                functionDeclaration=function,
                arguments=[{"value": arg} for arg in args],
                returnByValue=True,
            )
            or {}
        )
        if result.get("exceptionDetails"):
            raise cdp.CdpError("Browser operation failed")
        return result.get("result", {}).get("value")

    def observe(self):
        self.guard()
        try:
            data = self._call("function() { return this.observe(); }")
        except cdp.CdpError:
            # A completed navigation destroys the old world. Re-observing is safe;
            # an action is never retried here.
            self._initialize()
            data = self._call("function() { return this.observe(); }")
        if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
            raise cdp.CdpError("Invalid browser observation")
        if data.get("visibility") != "visible":
            raise cdp.CdpError("Browser tab changed; observe the active page")
        if not str(data.get("url", "")).startswith(("http://", "https://")):
            raise cdp.CdpError("This is no longer a web page; use standard computer tools")
        return data

    def act(self, operation, target=None, text=None):
        self.guard()
        return self._call(
            "function(op, target, text) { return this.act(op, target, text); }",
            operation,
            target,
            text,
        )

    def prepare_outgoing(self, url, text):
        self.guard()
        return self._call("function(url, text) { return this.prepareOutgoing(url, text); }", url, text)

    def submit_outgoing(self, url, text):
        self.guard()
        return self._call("function(url, text) { return this.submitOutgoing(url, text); }", url, text)

    def __exit__(self, *exc):
        if self.session:
            self.session.close()


def _visible_page_path(machine, port, tabs):
    pages = (
        [
            p
            for p in tabs
            if isinstance(p, dict)
            and p.get("type") == "page"
            and str(p.get("url", "")).startswith(("http://", "https://"))
        ]
        if isinstance(tabs, list)
        else []
    )
    if not 1 <= len(pages) <= 8:
        raise cdp.CdpError("Open a web page in a browser with at most eight web tabs")
    paths = [cdp._ws_path(p.get("webSocketDebuggerUrl", "")) for p in pages]
    if not all(paths):
        raise cdp.CdpError("Browser connection unavailable")
    if len(paths) == 1:
        return paths[0]
    visible = []
    for path in paths:
        with cdp._Session(machine, port, path) as session:
            result = (
                session.call(
                    "Runtime.evaluate", expression="document.visibilityState", returnByValue=True
                )
                or {}
            )
            if result.get("result", {}).get("value") == "visible":
                visible.append(path)
    if len(visible) != 1:
        raise cdp.CdpError("Active browser tab is ambiguous; use standard computer tools")
    return visible[0]
