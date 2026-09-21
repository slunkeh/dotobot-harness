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
            elements, unsupported, scroll_up: scrollY > 0,
            scroll_down: scrollY + innerHeight < document.documentElement.scrollHeight - 2};
        // Full field values and option identities stay local, including text past the model's cap.
        const signature = JSON.stringify([data, scrollX, scrollY, nodes.map(e => [e.value,
            Array.from(e.options || []).map(o => [o.value, o.label, o.disabled])]),
            Array.from(document.querySelectorAll('input,select,textarea')).map(e =>
                [e.type, e.name, e.value, e.checked, e.disabled, e.form?.action, e.form?.method])]);
        return {data, nodes, signature};
    }
    return {
        observe() { previous = read(); return previous.data; },
        act(operation, target, text) {
            if (!previous) return 'stale';
            const old = previous; previous = null; // consume before any side effect
            const current = read();
            if (old.signature !== current.signature || old.nodes.length !== current.nodes.length ||
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
        page = cdp._pick_page(tabs) if isinstance(tabs, list) else None
        if not page or not str(page.get("url", "")).startswith(("http://", "https://")):
            raise cdp.CdpError("Open a web page first")
        path = cdp._ws_path(page.get("webSocketDebuggerUrl", ""))
        if not path:
            raise cdp.CdpError("Browser connection unavailable")
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
        return data

    def act(self, operation, target=None, text=None):
        self.guard()
        return self._call(
            "function(op, target, text) { return this.act(op, target, text); }",
            operation,
            target,
            text,
        )

    def __exit__(self, *exc):
        if self.session:
            self.session.close()
