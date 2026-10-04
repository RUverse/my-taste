(() => {
  "use strict";

  // Skeleton cards stand in for titles while the next page or the next batch loads. They reuse a
  // card's own parts, so they are just as tall and follow the same display options.
  const skeletonCards = (count) =>
    Array.from({ length: count }, () => {
      const card = document.createElement("div");
      card.className = "skeleton-card";
      card.innerHTML = `<div class="poster skeleton-poster"></div>
        <div class="card-copy">
          <h2><span class="skeleton-line"></span></h2>
          <div class="card-meta"><span class="meta-year skeleton-line"></span><span class="meta-rating skeleton-line"></span></div>
          <p class="meta-genres"><span class="skeleton-line"></span></p>
          <p class="meta-people"><span class="skeleton-line"></span></p>
        </div>`;
      return card;
    });
  const columnsOf = (grid) => getComputedStyle(grid).gridTemplateColumns.split(" ").length;

  const browseContent = document.querySelector(".browse-content");
  const markNavigating = () => {
    document.body.classList.add("is-navigating");
    if (!browseContent || browseContent.querySelector(":scope > .skeleton-grid")) return;
    const grid = document.createElement("div");
    grid.className = "media-grid skeleton-grid";
    grid.setAttribute("aria-hidden", "true");
    browseContent.prepend(grid);
    grid.append(...skeletonCards(columnsOf(grid) * 3));
    browseContent.setAttribute("aria-busy", "true");
    window.scrollTo(0, 0);
  };
  window.addEventListener("pageshow", () => {
    document.body.classList.remove("is-navigating");
    browseContent?.querySelector(":scope > .skeleton-grid")?.remove();
    browseContent?.removeAttribute("aria-busy");
  });

  const searchForm = document.querySelector(".search-form");
  const searchToggle = document.querySelector(".search-toggle");
  const searchInput = document.querySelector(".search-input");

  if (searchForm && searchToggle && searchInput) {
    searchToggle.addEventListener("click", () => {
      const willOpen = !searchForm.classList.contains("is-open");
      searchForm.classList.toggle("is-open", willOpen);
      searchToggle.setAttribute("aria-expanded", String(willOpen));
      if (willOpen) {
        searchInput.focus();
      } else if (!searchInput.value) {
        searchToggle.focus();
      }
    });

    searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !searchInput.value) {
        searchForm.classList.remove("is-open");
        searchToggle.setAttribute("aria-expanded", "false");
        searchToggle.focus();
      }
    });

    // Live search: a pause in typing loads the results, so Enter is optional. The next page puts
    // the cursor back in the box and picks up anything typed while it loaded. Clearing the box
    // returns to the page the search started from.
    const liveSearchKey = "mytaste.live-search";
    const searchOriginKey = "mytaste.search-origin";
    const remember = (key, value) => {
      try {
        if (value === null) sessionStorage.removeItem(key);
        else sessionStorage.setItem(key, value);
      } catch {
        // Without storage, results still load; only the cursor and the way back are lost.
      }
    };
    const recall = (key) => {
      try {
        return sessionStorage.getItem(key);
      } catch {
        return null;
      }
    };
    const pageSearch = searchInput.defaultValue.trim();
    const media = searchForm.querySelector('input[name="media"]')?.value ?? "all";
    const searchUrl = (text) => {
      if (media === "game") {
        const target = new URL(searchForm.action);
        new FormData(searchForm).forEach((value, key) => {
          if (value && !["media", "q", "page"].includes(key)) target.searchParams.set(key, value);
        });
        if (text) target.searchParams.set("q", text);
        return target.pathname + target.search;
      }
      const target = new URL(text ? "/" : recall(searchOriginKey) || "/", window.location.origin);
      if (text) target.searchParams.set("q", text);
      if (media === "all") target.searchParams.delete("media");
      else target.searchParams.set("media", media);
      return target.pathname + target.search;
    };
    const rememberOrigin = () => {
      if (!pageSearch) remember(searchOriginKey, window.location.pathname + window.location.search);
    };

    let searchTimer;
    let searchNavigating = false;
    const runSearch = () => {
      const text = searchInput.value.trim();
      // A single letter matches too much to be worth a page load; Enter still searches for it.
      if (text === pageSearch || text.length === 1) return;
      searchNavigating = true;
      markNavigating();
      if (!text && history.state?.searchFromOrigin) {
        history.back();
      } else if (pageSearch) {
        // Refining a search replaces it in the history, so Back leaves the search in one step.
        window.location.replace(searchUrl(text));
      } else {
        rememberOrigin();
        window.location.assign(searchUrl(text));
      }
    };
    const scheduleSearch = () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(runSearch, 400);
    };
    searchInput.addEventListener("input", (event) => {
      if (!event.isComposing) scheduleSearch();
    });
    searchInput.addEventListener("compositionend", scheduleSearch);
    searchForm.addEventListener("submit", () => {
      window.clearTimeout(searchTimer);
      rememberOrigin();
    });
    window.addEventListener("pagehide", () => {
      if (!searchNavigating) return;
      // Whether Back from the results is the page the search started on, so clearing can go there.
      const fromOrigin = !pageSearch || Boolean(history.state?.searchFromOrigin);
      remember(liveSearchKey, JSON.stringify({ typed: searchInput.value, fromOrigin }));
    });

    const resumeTyping = () => {
      let carried = null;
      try {
        carried = JSON.parse(recall(liveSearchKey));
      } catch {
        carried = null;
      }
      remember(liveSearchKey, null);
      if (typeof carried?.typed !== "string") return;
      const { fromOrigin } = carried;
      // Keys pressed here before this script ran follow the text carried over from the last page.
      const early = searchInput.value.startsWith(searchInput.defaultValue)
        ? searchInput.value.slice(searchInput.defaultValue.length)
        : "";
      const typed = carried.typed + early;
      if (pageSearch && fromOrigin) history.replaceState({ ...history.state, searchFromOrigin: true }, "");
      searchForm.classList.add("is-open");
      searchToggle.setAttribute("aria-expanded", "true");
      if (typed.trim() !== pageSearch) scheduleSearch();
      searchInput.value = typed;
      searchInput.focus();
      searchInput.setSelectionRange(typed.length, typed.length);
    };
    window.addEventListener("pageshow", (event) => {
      searchNavigating = false;
      // Back restores the page as it was left, with the search that was typed into it.
      if (event.persisted) {
        searchInput.value = searchInput.defaultValue;
        resumeTyping();
      }
    });
    resumeTyping();
  }

  // Popovers: a button with data-popover-button shows the element named by aria-controls.
  const popoverButtons = Array.from(document.querySelectorAll("[data-popover-button]"));
  // Escape closes an open popover; a dialog around it ignores that same key press.
  let popoverEscapedAt = -Infinity;
  const escapeClosedPopover = (event) => event.timeStamp - popoverEscapedAt < 200;
  const popoverOf = (button) => document.getElementById(button.getAttribute("aria-controls"));

  const closePopover = (button, { restoreFocus = false } = {}) => {
    const popover = popoverOf(button);
    if (!popover || popover.hidden) return;
    popover.hidden = true;
    button.setAttribute("aria-expanded", "false");
    popover.dispatchEvent(new Event("popoverclose"));
    if (restoreFocus) button.focus();
  };

  const openPopover = (button) => {
    popoverButtons.forEach((other) => other !== button && closePopover(other));
    const popover = popoverOf(button);
    if (!popover) return;
    popover.hidden = false;
    button.setAttribute("aria-expanded", "true");
    popover.querySelector("a:not([hidden]), button:not([hidden]), input")?.focus();
  };

  popoverButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (popoverOf(button)?.hidden) openPopover(button);
      else closePopover(button);
    });
  });
  document.addEventListener("click", (event) => {
    // The path is fixed when the click starts, so it still counts a menu item that re-rendered.
    const path = event.composedPath();
    popoverButtons.forEach((button) => {
      const anchor = button.closest(".popover-anchor");
      if (anchor && !path.includes(anchor)) closePopover(button);
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const open = popoverButtons.find((button) => !popoverOf(button)?.hidden);
    if (open) {
      popoverEscapedAt = event.timeStamp;
      event.stopPropagation();
      closePopover(open, { restoreFocus: true });
    }
  });

  const brandForm = document.querySelector("[data-site-title-form]");
  const brandActions = document.querySelector("[data-brand-actions]");
  const brandPopover = brandForm?.closest(".popover");
  if (brandForm && brandActions && brandPopover) {
    const input = brandForm.querySelector("input");
    const error = brandForm.querySelector("[data-site-title-error]");
    const showActions = () => {
      brandForm.hidden = true;
      brandActions.hidden = false;
      error.hidden = true;
    };
    document.querySelector("[data-rename-site]")?.addEventListener("click", () => {
      brandActions.hidden = true;
      brandForm.hidden = false;
      input.focus();
      input.select();
    });
    brandForm.querySelector("[data-rename-cancel]")?.addEventListener("click", () => {
      input.value = document.querySelector("[data-site-title]").textContent;
      showActions();
      brandActions.querySelector("[data-rename-site]")?.focus();
    });
    brandPopover.addEventListener("popoverclose", showActions);
    brandForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const response = await fetch("/api/preferences/site-title", {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ title: input.value }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Could not rename the site");
        const previous = document.querySelector("[data-site-title]").textContent;
        document.querySelectorAll("[data-site-title]").forEach((node) => {
          node.textContent = payload.site_title;
        });
        if (document.title.endsWith(previous)) {
          document.title = `${document.title.slice(0, -previous.length)}${payload.site_title}`;
        }
        input.value = payload.site_title;
        const button = document.querySelector('[aria-controls="brand-popover"]');
        if (button) closePopover(button, { restoreFocus: true });
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
      }
    });
  }

  const sidebar = document.querySelector("#browse-sidebar");
  const sidebarToggles = Array.from(document.querySelectorAll("[data-sidebar-toggle]"));
  const rail = document.querySelector(".sidebar-rail");

  if (sidebar && sidebarToggles.length) {
    const overlayQuery = window.matchMedia("(max-width: 900px)");
    const mobileToggle = document.querySelector(".browse-toolbar [data-sidebar-toggle]");

    const sidebarIsOpen = () =>
      overlayQuery.matches
        ? document.body.classList.contains("sidebar-overlay-open")
        : document.body.dataset.sidebar === "open";

    const syncSidebar = () => {
      const open = sidebarIsOpen();
      sidebar.inert = !open;
      if (rail) rail.inert = open || overlayQuery.matches;
      sidebarToggles.forEach((toggle) => toggle.setAttribute("aria-expanded", String(open)));
    };

    const setSidebarOpen = (open, { focus = true } = {}) => {
      const hadFocus = sidebar.contains(document.activeElement);
      if (overlayQuery.matches) {
        document.body.classList.toggle("sidebar-overlay-open", open);
      } else {
        document.body.dataset.sidebar = open ? "open" : "closed";
        fetch("/api/preferences/display", {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ sidebar_open: open }),
        }).catch(() => {});
        window.dispatchEvent(new Event("sidebarchange"));
      }
      syncSidebar();
      if (!focus) return;
      if (open) {
        sidebar.querySelector(overlayQuery.matches ? "[data-sidebar-close]" : ".sidebar-collapse")?.focus();
      } else if (hadFocus) {
        (overlayQuery.matches ? mobileToggle : rail?.querySelector("[data-sidebar-toggle]"))?.focus();
      }
    };

    sidebarToggles.forEach((toggle) => {
      toggle.addEventListener("click", () => setSidebarOpen(!sidebarIsOpen()));
    });
    rail?.querySelectorAll("[data-rail-section]").forEach((button) => {
      button.addEventListener("click", () => {
        const section = document.getElementById(button.dataset.railSection);
        setSidebarOpen(true, { focus: false });
        if (section instanceof HTMLDetailsElement) section.open = true;
        disclosures.get(button.dataset.railSection)?.(true);
        if (!section) return;
        section.scrollIntoView({ block: "nearest" });
        const controls = section.querySelectorAll("summary, select, input:not([type='hidden']), button, a");
        Array.from(controls)
          .find((control) => control.getClientRects().length > 0)
          ?.focus({ preventScroll: true });
      });
    });
    document.querySelectorAll("[data-sidebar-close]").forEach((button) => {
      button.addEventListener("click", () => setSidebarOpen(false));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && overlayQuery.matches && sidebarIsOpen()) {
        setSidebarOpen(false);
      }
    });
    overlayQuery.addEventListener("change", () => {
      document.body.classList.remove("sidebar-overlay-open");
      syncSidebar();
    });
    syncSidebar();
  }

  // Collapsible sections start collapsed; Sources then shows just the service icons. After a
  // source is switched on or off, the reloaded page keeps the section open.
  const disclosures = new Map();
  document.querySelectorAll("[data-disclosure]").forEach((button) => {
    const name = button.dataset.disclosure;
    const body = document.querySelector(`[data-disclosure-body="${name}"]`);
    const summary = document.querySelector(`[data-disclosure-summary="${name}"]`);
    const keepOpenKey = `mytaste.keep-open.${name}`;
    const setOpen = (open) => {
      button.setAttribute("aria-expanded", String(open));
      if (body) body.hidden = !open;
      if (summary) summary.hidden = open;
    };
    button.addEventListener("click", () => setOpen(button.getAttribute("aria-expanded") !== "true"));
    body?.addEventListener("change", () => {
      try {
        sessionStorage.setItem(keepOpenKey, "1");
      } catch {
        // Without storage the section simply starts collapsed again.
      }
    });
    let keepOpen = false;
    try {
      keepOpen = sessionStorage.getItem(keepOpenKey) === "1";
      sessionStorage.removeItem(keepOpenKey);
    } catch {
      keepOpen = false;
    }
    setOpen(keepOpen);
    disclosures.set(button.closest(".sidebar-section")?.id, setOpen);
  });

  // Filter rules: "+" reveals a rule's controls; it only applies once a value is chosen.
  const filterButton = document.querySelector('[aria-controls="filter-popover"]');
  document.querySelectorAll("[data-add-filter]").forEach((choice) => {
    choice.addEventListener("click", () => {
      const rule = document.querySelector(`[data-filter-rule="${choice.dataset.addFilter}"]`);
      if (!rule) return;
      rule.hidden = false;
      choice.hidden = true;
      if (filterButton) {
        closePopover(filterButton);
        filterButton.hidden = !document.querySelector("[data-add-filter]:not([hidden])");
      }
      rule.querySelector("input:not([type='hidden']), select")?.focus();
    });
  });
  document.querySelectorAll("[data-remove-filter]").forEach((remove) => {
    remove.addEventListener("click", () => {
      const name = remove.dataset.removeFilter;
      document.querySelector(`[data-filter-rule="${name}"]`).hidden = true;
      document.querySelector(`[data-add-filter="${name}"]`).hidden = false;
      if (filterButton) {
        filterButton.hidden = false;
        filterButton.focus();
      }
    });
  });

  // Collection bar: tabs that do not fit move into the "Show all" popover.
  const collectionBar = document.querySelector("[data-collection-bar]");
  if (collectionBar) {
    const tabs = Array.from(collectionBar.querySelectorAll("[data-collection-tabs] [data-collection]"));
    const dividers = Array.from(collectionBar.querySelectorAll("[data-collection-divider]"));
    const more = document.querySelector("[data-collection-more]");
    const addCollection = document.querySelector("[data-collection-add]");
    const overflowLinks = new Map(
      Array.from(document.querySelectorAll("[data-collection-overflow]")).map((link) => [
        link.dataset.collectionOverflow,
        link,
      ]),
    );
    const fits = () => collectionBar.scrollWidth <= collectionBar.clientWidth + 1;

    const layoutTabs = () => {
      collectionBar.classList.add("is-measured");
      tabs.forEach((tab) => {
        tab.hidden = false;
      });
      dividers.forEach((divider) => {
        divider.hidden = false;
      });
      more.hidden = true;
      if (addCollection) addCollection.hidden = false;
      if (!fits()) {
        more.hidden = false;
        if (addCollection) addCollection.hidden = true;
        for (let index = tabs.length - 1; index >= 0 && !fits(); index -= 1) {
          tabs[index].hidden = true;
        }
      }
      dividers.forEach((divider) => {
        const before = divider.previousElementSibling;
        const after = divider.nextElementSibling;
        divider.hidden = !before || before.hidden || !after || after.hidden;
      });
      tabs.forEach((tab) => {
        const link = overflowLinks.get(tab.dataset.collection);
        if (link) link.hidden = !tab.hidden;
      });
    };

    layoutTabs();
    new ResizeObserver(layoutTabs).observe(collectionBar);
    document.fonts?.ready.then(layoutTabs);
  }

  // Collection editor: creates a collection, or edits or deletes the one being viewed.
  const editor = document.querySelector("#collection-editor");
  const editorForm = editor?.querySelector("[data-collection-form]");
  let editorState = null;

  const openCollectionEditor = ({ collection = null, onSaved }) => {
    if (!editor || !editorForm) return;
    editorState = { collection, onSaved };
    editorForm.reset();
    editorForm.elements.name.value = collection?.name ?? "";
    editorForm.elements.description.value = collection?.description ?? "";
    editorForm.elements.default_sort.value = collection?.default_sort ?? "added";
    const icon = editorForm.querySelector(`input[name="icon"][value="${collection?.icon ?? ""}"]`);
    if (icon) icon.checked = true;
    editor.querySelector("[data-editor-title]").textContent = collection ? "Edit collection" : "New collection";
    editor.querySelector("[data-editor-submit]").textContent = collection ? "Save" : "Create";
    editor.querySelector("[data-editor-delete]").hidden = !collection;
    editor.querySelector("[data-editor-error]").hidden = true;
    editor.showModal();
    editorForm.elements.name.focus();
  };

  if (editor && editorForm) {
    const error = editor.querySelector("[data-editor-error]");
    const showError = (message) => {
      error.textContent = message;
      error.hidden = false;
    };
    editor.querySelectorAll("[data-editor-close]").forEach((button) => {
      button.addEventListener("click", () => editor.close());
    });
    editor.addEventListener("click", (event) => {
      if (event.target === editor) editor.close();
    });
    editorForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(editorForm));
      if (!values.name.trim()) {
        showError("Give the collection a name.");
        editorForm.elements.name.focus();
        return;
      }
      const existing = editorState?.collection;
      try {
        const response = await fetch(existing ? `/api/collections/${existing.id}` : "/api/collections", {
          method: existing ? "PATCH" : "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify(values),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "The collection could not be saved.");
        editor.close();
        await editorState?.onSaved?.(payload.collection);
      } catch (failure) {
        showError(failure.message);
      }
    });
    editor.querySelector("[data-editor-delete]").addEventListener("click", async () => {
      const existing = editorState?.collection;
      if (!existing || !window.confirm(`Delete “${existing.name}”? The titles in it are not affected.`)) {
        return;
      }
      const response = await fetch(`/api/collections/${existing.id}`, { method: "DELETE" });
      if (response.ok) {
        window.location.assign("/");
      } else {
        showError("The collection could not be deleted.");
      }
    });
  }

  document.querySelectorAll("[data-new-collection]").forEach((button) => {
    button.addEventListener("click", () => {
      const popoverButton = document.querySelector('[aria-controls="collections-popover"]');
      if (popoverButton) closePopover(popoverButton);
      openCollectionEditor({ onSaved: (collection) => window.location.assign(collection.url) });
    });
  });
  document.querySelector("[data-edit-collection]")?.addEventListener("click", (event) => {
    const collection = JSON.parse(event.currentTarget.dataset.editCollection);
    openCollectionEditor({ collection, onSaved: () => window.location.reload() });
  });

  document.querySelectorAll(".browse-sidebar a, .active-filters a, .collection-bar a").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (!event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0) {
        markNavigating();
      }
    });
  });

  const filterForm = document.querySelector("[data-filter-form]");

  if (filterForm) {
    const sourceBoxes = Array.from(filterForm.querySelectorAll(".source-input"));

    const sortOrder = filterForm.querySelector("[data-sort-order]");
    let sortChanged = false;

    filterForm.addEventListener("change", (event) => {
      // Search boxes only suggest; picking a suggestion applies it.
      if (event.target.closest("[data-suggest]")) return;
      if (sourceBoxes.includes(event.target) && !sourceBoxes.some((box) => box.checked)) {
        event.target.checked = true;
        return;
      }
      // A new sort starts in its natural direction: newest, most popular, or A to Z first.
      sortChanged = event.target.matches("[data-sort-field]");
      filterForm.requestSubmit();
    });

    filterForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const params = new URLSearchParams();
      new FormData(filterForm).forEach((value, key) => {
        if (key === "providers" || key === "libraries" || value === "") return;
        if (sortChanged && filterForm.elements[key] === sortOrder) return;
        // Defaults are left out to keep links short.
        if (filterForm.elements[key]?.dataset?.default === value) return;
        // Filters with several values list them in one parameter: genre=drama,comedy.
        const values = params.has(key) ? params.get(key).split(",") : [];
        if (!values.includes(value)) params.set(key, [...values, value].join(","));
      });
      ["providers", "libraries"].forEach((key) => {
        const boxes = sourceBoxes.filter((box) => box.name === key);
        const checked = boxes.filter((box) => box.checked).map((box) => box.value);
        if (boxes.length && checked.length !== boxes.length) {
          params.set(key, checked.length ? checked.join(",") : "none");
        }
      });
      markNavigating();
      const path = new URL(filterForm.action).pathname;
      // Commas need no escaping in a query string and keep lists readable.
      window.location.assign(params.size ? `${path}?${String(params).replaceAll("%2C", ",")}` : path);
    });
  }

  // People and keyword filters: suggestions as you type; choosing one applies the filter.
  document.querySelectorAll("[data-suggest]").forEach((box) => {
    const input = box.querySelector(".suggest-input");
    const list = box.querySelector(".suggest-list");
    const status = box.querySelector(".suggest-status");
    let results = [];
    let active = -1;
    let timer = 0;
    let request = null;

    const close = () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    };
    const highlight = (index) => {
      const options = Array.from(list.children);
      active = options.length ? (index + options.length) % options.length : -1;
      options.forEach((option, position) => option.setAttribute("aria-selected", String(position === active)));
      if (active >= 0) {
        input.setAttribute("aria-activedescendant", options[active].id);
        options[active].scrollIntoView({ block: "nearest" });
      }
    };
    const choose = (result) => {
      if (!filterForm || !result) return;
      const chosen = document.createElement("input");
      chosen.type = "hidden";
      chosen.name = box.dataset.suggestParam;
      chosen.value = String(result.id);
      filterForm.append(chosen);
      close();
      input.value = result.name;
      filterForm.requestSubmit();
    };
    const render = () => {
      // Re-rendering replaces the buttons, so the focused one hands focus to its replacement.
      const focused = Array.from(list.children).indexOf(document.activeElement);
      // Names stay aligned when only some collections have an icon.
      const anyIcon = menu.collections.some((collection) => collection.icon);
      list.replaceChildren(
        ...results.map((result, index) => {
          const option = document.createElement("li");
          option.id = `${list.id}-${index}`;
          option.setAttribute("role", "option");
          option.setAttribute("aria-selected", "false");
          const name = document.createElement("span");
          name.textContent = result.name;
          option.append(name);
          if (result.detail) {
            const detail = document.createElement("small");
            detail.textContent = result.detail;
            option.append(detail);
          }
          // Keep focus in the box so the choice is not lost to a blur.
          option.addEventListener("mousedown", (event) => event.preventDefault());
          option.addEventListener("click", () => choose(result));
          return option;
        }),
      );
      const open = results.length > 0;
      list.hidden = !open;
      input.setAttribute("aria-expanded", String(open));
      active = -1;
    };
    const search = async (text) => {
      request?.abort();
      request = new AbortController();
      status.textContent = "Searching…";
      try {
        const response = await fetch(
          `/api/filters/suggest?${new URLSearchParams({ kind: box.dataset.suggest, q: text })}`,
          { headers: { Accept: "application/json" }, signal: request.signal },
        );
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Search failed");
        results = payload.results || [];
        status.textContent = results.length ? `${results.length} suggestions` : "No matches";
      } catch (error) {
        if (error.name === "AbortError") return;
        results = [];
        status.textContent = "Search is unavailable right now";
      }
      render();
    };

    input.addEventListener("input", () => {
      window.clearTimeout(timer);
      const text = input.value.trim();
      if (text.length < 2) {
        request?.abort();
        results = [];
        status.textContent = "";
        render();
        return;
      }
      timer = window.setTimeout(() => search(text), 250);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (list.hidden) return;
        event.preventDefault();
        highlight(active + (event.key === "ArrowDown" ? 1 : -1));
      } else if (event.key === "Enter") {
        // Never submit the form with the typed text itself.
        event.preventDefault();
        if (!list.hidden) choose(results[Math.max(active, 0)]);
      } else if (event.key === "Escape" && !list.hidden) {
        event.preventDefault();
        event.stopPropagation();
        close();
      }
    });
    input.addEventListener("blur", close);
  });

  // Every details view (titles, games, the game page) scrolls itself and repeats its title beside
  // the back button once the heading has scrolled up under the bar.
  document.querySelectorAll(".media-details").forEach((details) => {
    const heading = details.querySelector("[data-detail-title]");
    const barTitle = details.querySelector("[data-detail-bar-title]");
    const bar = details.querySelector(".media-detail-topbar");
    if (!heading || !barTitle || !bar) return;
    let frame = 0;
    const update = () => {
      frame = 0;
      const headingBottom = heading.getBoundingClientRect().bottom;
      const scrolledPast = details.scrollTop > 0 && headingBottom <= bar.getBoundingClientRect().bottom;
      details.classList.toggle("is-title-scrolled", scrolledPast);
    };
    const scheduleUpdate = () => {
      frame ||= requestAnimationFrame(update);
    };
    const syncTitle = () => {
      barTitle.textContent = heading.textContent.trim();
      scheduleUpdate();
    };
    syncTitle();
    new MutationObserver(syncTitle).observe(heading, { childList: true, characterData: true, subtree: true });
    details.addEventListener("scroll", scheduleUpdate, { passive: true });
    window.addEventListener("resize", scheduleUpdate);
  });

  const detailDialog = document.querySelector("#media-details");
  const detailPoster = detailDialog?.querySelector("[data-detail-poster]");
  const detailTitle = detailDialog?.querySelector("[data-detail-title]");
  const detailKind = detailDialog?.querySelector("[data-detail-kind]");
  const detailCredit = detailDialog?.querySelector("[data-detail-credit]");
  const detailCreditLabel = detailDialog?.querySelector("[data-detail-credit-label]");
  const detailCreditNames = detailDialog?.querySelector("[data-detail-credit-names]");
  const detailRuntime = detailDialog?.querySelector("[data-detail-runtime]");
  const detailRating = detailDialog?.querySelector("[data-detail-rating]");
  const detailStatus = detailDialog?.querySelector("[data-detail-status]");
  const detailContent = detailDialog?.querySelector("[data-detail-content]");
  const detailGenres = detailDialog?.querySelector("[data-detail-genres]");
  const detailOverview = detailDialog?.querySelector("[data-detail-overview]");
  const detailCast = detailDialog?.querySelector("[data-detail-cast]");
  const detailCastNames = detailDialog?.querySelector("[data-detail-cast-names]");
  const detailCastList = detailDialog?.querySelector("[data-detail-cast-list]");
  const detailBackdrop = detailDialog?.querySelector("[data-detail-backdrop]");
  const detailVideo = detailDialog?.querySelector("[data-detail-video]");
  const detailSound = detailDialog?.querySelector("[data-detail-sound]");
  const detailTrailer = detailDialog?.querySelector("[data-detail-trailer]");
  const detailPlayGroup = detailDialog?.querySelector("[data-play-group]");
  const detailPlay = detailDialog?.querySelector("[data-detail-play]");
  const detailPlayLabel = detailDialog?.querySelector("[data-detail-play-label]");
  const detailPlaySource = detailDialog?.querySelector("[data-detail-play-source]");
  const detailWatchAlternatives = detailDialog?.querySelector("[data-watch-alternatives]");
  const detailEpisodes = detailDialog?.querySelector("[data-detail-episodes]");
  const detailEpisodesSummary = detailDialog?.querySelector("[data-episodes-summary]");
  const detailEpisodesStatus = detailDialog?.querySelector("[data-episodes-status]");
  const detailSeasons = detailDialog?.querySelector("[data-episodes-seasons]");
  const detailNeighbors = Array.from(detailDialog?.querySelectorAll("[data-detail-neighbor]") ?? []);
  const detailCards = Array.from(document.querySelectorAll(".media-card"));
  const detailCache = new Map();
  const folderIcon =
    '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4.2l2 2H19a1.5 1.5 0 0 1 1.5 1.5v7A1.5 1.5 0 0 1 19 18H5a1.5 1.5 0 0 1-1.5-1.5v-9Z"></path></svg>';
  // What the Play button offers: the local file first, otherwise the best-ranked service.
  let localPlay = null;
  let watchOptions = [];
  let streamPlayLabel = "Play";
  // A series' runtime is the length of the episode Play opens, once the episode list is in.
  let showRuntime = null;
  let episodeRuntime = null;
  let watchUrl = "";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let activeDetailCard;
  let detailRevision = 0;
  let posterMotionRevision = 0;

  const setTrailerMuted = (muted, sendCommand = false) => {
    detailSound.dataset.muted = String(muted);
    detailSound.setAttribute("aria-pressed", String(!muted));
    detailSound.setAttribute("aria-label", muted ? "Unmute trailer" : "Mute trailer");
    detailSound.title = muted ? "Unmute trailer" : "Mute trailer";
    if (sendCommand) {
      detailVideo.querySelector("iframe")?.contentWindow?.postMessage(
        JSON.stringify({ event: "command", func: muted ? "mute" : "unMute", args: [] }),
        "https://www.youtube-nocookie.com",
      );
    }
  };

  // A copy of a card's cover image, or a lettered placeholder when it has none.
  const posterVisual = (card) => {
    const visual = card.querySelector(".poster")?.querySelector("img, .poster-placeholder")?.cloneNode(true);
    if (visual) {
      visual.removeAttribute("loading");
      return visual;
    }
    const placeholder = document.createElement("div");
    placeholder.className = "poster-placeholder";
    placeholder.setAttribute("aria-hidden", "true");
    const initial = document.createElement("span");
    initial.textContent = (card.dataset.title || "").charAt(0) || "?";
    placeholder.append(initial);
    return placeholder;
  };

  const renderGenres = (genres) => {
    detailGenres.replaceChildren();
    genres.forEach((genre) => {
      const chip = document.createElement("span");
      chip.textContent = genre;
      detailGenres.append(chip);
    });
  };

  const formatRating = (value) => {
    const rating = Number(value || 0);
    return rating ? `${rating.toFixed(1)} ★` : "";
  };

  const setKind = (years, label) => {
    detailKind.textContent = [years === "—" ? "" : years, label].filter(Boolean).join(" · ");
  };

  const setCredit = (label, names) => {
    detailCredit.hidden = !names;
    detailCredit.title = names ? `${label}: ${names}` : "";
    detailCreditLabel.textContent = label ? `${label}:` : "";
    detailCreditNames.textContent = names;
  };

  // The shadows follow the OS theme, so they are read from the stylesheet when a flight starts.
  const posterShadows = () => {
    const style = getComputedStyle(detailDialog);
    return {
      card: style.getPropertyValue("--poster-shadow").trim(),
      detail: style.getPropertyValue("--detail-poster-shadow").trim(),
    };
  };

  // FLIP the dialog's own poster between its resting place and a card's poster, so the cover
  // reads as one object moving rather than a copy cross-fading with the original.
  const flyPoster = (cardRect, { reverse = false } = {}) => {
    detailPoster.getAnimations().forEach((animation) => animation.cancel());
    const restRect = detailPoster.getBoundingClientRect();
    if (!cardRect?.width || !restRect.width || reducedMotion.matches || !("animate" in Element.prototype)) {
      return null;
    }
    const scaleX = cardRect.width / restRect.width;
    const scaleY = cardRect.height / restRect.height;
    const shadows = posterShadows();
    const atCard = {
      transform: `translate(${cardRect.left - restRect.left}px, ${cardRect.top - restRect.top}px) scale(${scaleX}, ${scaleY})`,
      borderRadius: `${11 / scaleX}px / ${11 / scaleY}px`,
      boxShadow: shadows.card,
    };
    const atRest = {
      transform: "translate(0px, 0px) scale(1, 1)",
      borderRadius: "12px / 12px",
      boxShadow: shadows.detail,
    };
    return detailPoster.animate(reverse ? [atRest, atCard] : [atCard, atRest], {
      duration: reverse ? 420 : 560,
      easing: reverse ? "cubic-bezier(0.32, 0, 0.18, 1)" : "cubic-bezier(0.16, 1, 0.3, 1)",
      fill: "both",
    });
  };

  const settleFlight = (flight, motionRevision) => {
    flight?.finished
      .then(() => {
        if (motionRevision === posterMotionRevision) {
          flight.cancel();
        }
      })
      .catch(() => {
        // A close that starts mid-flight cancels this animation.
      });
  };

  const resetDetailContent = (card) => {
    detailPoster.replaceChildren(posterVisual(card));
    detailTitle.textContent = card.dataset.title || "Loading…";
    setKind(card.dataset.year, card.dataset.mediaLabel);
    // The card may already have loaded its director line.
    const people = card.querySelector('.meta-people[data-loaded="true"]')?.textContent.trim() || "";
    setCredit("", people);
    detailRuntime.textContent = "";
    detailRating.textContent = formatRating(card.dataset.rating);
    detailStatus.textContent = "Loading trailer and cast…";
    let genres = [];
    try {
      const parsedGenres = JSON.parse(card.dataset.genres || "[]");
      genres = Array.isArray(parsedGenres) ? parsedGenres : [];
    } catch {
      genres = [];
    }
    renderGenres(genres);
    detailOverview.textContent = card.dataset.overview || "No description is available yet.";
    detailContent.hidden = false;
    renderCast([]);
    detailBackdrop.onload = null;
    detailBackdrop.removeAttribute("src");
    detailBackdrop.classList.remove("has-image");
    detailVideo.replaceChildren();
    detailVideo.classList.remove("is-active");
    detailSound.hidden = true;
    setTrailerMuted(true);
    detailTrailer.hidden = true;
    detailTrailer.removeAttribute("href");
    localPlay = card.dataset.playUrl
      ? { url: card.dataset.playUrl, label: card.dataset.resume ? "Resume" : "Play" }
      : null;
    watchOptions = [];
    streamPlayLabel = "Play";
    showRuntime = null;
    episodeRuntime = null;
    watchUrl = "";
    renderPlay();
    const isSeries = card.dataset.detailUrl?.startsWith("/api/items/tv/") ?? false;
    detailDialog.classList.toggle("is-series", isSeries);
    detailEpisodes.hidden = !isSeries;
    detailEpisodesSummary.textContent = "";
    detailEpisodesStatus.textContent = isSeries ? "Loading episodes…" : "";
    detailSeasons.replaceChildren();
  };

  const providerLogo = (option, className) => {
    const logo = document.createElement("span");
    logo.className = className;
    if (option.logo_url) {
      const image = document.createElement("img");
      image.src = option.logo_url;
      image.alt = "";
      logo.append(image);
    } else {
      logo.textContent = option.name?.charAt(0) || "?";
    }
    return logo;
  };

  const watchTitle = (option) =>
    option.direct ? `Watch on ${option.name}` : `Find ${option.name} offers on TMDB`;

  // Local titles play in MyTaste's own player, marked with a folder; the services that carry
  // the title sit beside it. Without a local file, the best-ranked service takes the button.
  const renderPlay = () => {
    const [primary, ...others] = watchOptions;
    const alternatives = localPlay ? watchOptions : others;
    detailPlayGroup.hidden = !localPlay && !primary;
    detailPlaySource.replaceChildren();
    if (localPlay) {
      detailPlay.href = localPlay.url;
      detailPlay.removeAttribute("target");
      detailPlay.removeAttribute("rel");
      detailPlayLabel.textContent = localPlay.label;
      detailPlay.title = `${localPlay.label} from your library`;
      detailPlaySource.innerHTML = folderIcon;
      detailPlaySource.className = "play-source play-source-local";
    } else if (primary) {
      detailPlay.href = primary.url;
      detailPlay.target = "_blank";
      detailPlay.rel = "noopener noreferrer";
      detailPlayLabel.textContent = streamPlayLabel;
      detailPlay.title = watchTitle(primary);
      detailPlaySource.append(providerLogo(primary, "watch-provider-logo"));
      detailPlaySource.className = "play-source";
    } else {
      detailPlay.removeAttribute("href");
    }
    detailPlay.setAttribute("aria-label", detailPlay.title);
    detailWatchAlternatives.replaceChildren(
      ...alternatives.map((option) => {
        const link = document.createElement("a");
        link.className = "watch-alternative";
        link.href = option.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = watchTitle(option);
        link.setAttribute("aria-label", watchTitle(option));
        link.append(providerLogo(option, "watch-provider-logo"));
        return link;
      }),
    );
  };

  const renderWatchOptions = (options) => {
    watchOptions = options.filter((option) => option.url?.startsWith("https://"));
    watchUrl = watchOptions[0]?.url || "";
    renderPlay();
    detailSeasons.querySelectorAll("[data-episode-link]").forEach(linkEpisode);
  };

  const linkEpisode = (card) => {
    if (!watchUrl || card.dataset.upcoming === "true" || card.dataset.local === "true") {
      return;
    }
    card.href = watchUrl;
    card.target = "_blank";
    card.rel = "noopener noreferrer";
    card.setAttribute("aria-label", card.dataset.label);
  };

  const airDateFormat = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });

  const formatAirDate = (value) => {
    const parsed = /^\d{4}-\d{2}-\d{2}$/.test(value || "") ? new Date(`${value}T00:00:00`) : null;
    return parsed && !Number.isNaN(parsed.getTime()) ? airDateFormat.format(parsed) : "";
  };

  const renderEpisode = (episode, seasonNumber, today) => {
    const item = document.createElement("li");
    const card = document.createElement("a");
    card.className = "episode-card";
    card.dataset.episodeLink = "";
    // Undated regular episodes are announced but not out yet; undated specials are just old.
    const upcoming = episode.air_date ? episode.air_date > today : seasonNumber > 0;
    card.dataset.upcoming = String(upcoming);

    const still = document.createElement("div");
    still.className = "episode-still";
    if (episode.still_url) {
      const image = document.createElement("img");
      image.src = episode.still_url;
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      still.append(image);
    }
    const number = document.createElement("span");
    number.className = "episode-number";
    number.textContent = String(episode.episode_number);
    still.append(number);
    if (episode.watched) {
      const watched = document.createElement("span");
      watched.className = "episode-watched";
      watched.title = "Watched";
      watched.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m6 12.5 4 4 8-9"></path></svg>';
      still.append(watched);
    } else if (episode.progress > 0) {
      const progress = document.createElement("span");
      progress.className = "card-progress";
      const bar = document.createElement("span");
      bar.style.width = `${Math.round(episode.progress * 1000) / 10}%`;
      progress.append(bar);
      still.append(progress);
    }
    if (episode.in_library) {
      const local = document.createElement("span");
      local.className = "episode-local";
      local.title = "In your local library";
      local.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4.2l2 2H19a1.5 1.5 0 0 1 1.5 1.5v7A1.5 1.5 0 0 1 19 18H5a1.5 1.5 0 0 1-1.5-1.5v-9Z"></path></svg>';
      still.append(local);
    }
    const play = document.createElement("span");
    play.className = "episode-play";
    play.setAttribute("aria-hidden", "true");
    play.innerHTML = '<svg viewBox="0 0 24 24"><path d="M8 5.5v13l10.5-6.5L8 5.5Z"></path></svg>';
    still.append(play);

    const copy = document.createElement("div");
    copy.className = "episode-copy";
    const name = document.createElement("strong");
    name.textContent = episode.name || `Episode ${episode.episode_number}`;
    const meta = document.createElement("small");
    const airDate = formatAirDate(episode.air_date);
    meta.textContent = upcoming
      ? airDate ? `Coming ${airDate}` : "Not yet aired"
      : [runtimeLabel(episode.runtime_minutes), airDate].filter(Boolean).join(" · ");
    copy.append(name, meta);
    if (episode.overview) {
      card.title = episode.overview;
    }
    const label = seasonNumber === 0 ? "Special" : `Season ${seasonNumber}, episode`;
    const context = [
      `${label} ${episode.episode_number}: ${name.textContent}`,
      meta.textContent,
      episode.in_library ? "in your local library" : "",
      episode.watched ? "watched" : "",
    ].filter(Boolean);
    card.dataset.label = context.join(", ");
    card.append(still, copy);
    if (episode.play_url) {
      card.dataset.local = "true";
      card.href = episode.play_url;
      card.setAttribute("aria-label", `Play ${card.dataset.label}`);
    }
    linkEpisode(card);
    item.append(card);
    return item;
  };

  const renderSeasons = (seasons) => {
    const today = new Date().toISOString().slice(0, 10);
    const regular = seasons.filter((season) => season.season_number > 0);
    const episodeCount = regular.reduce((total, season) => total + season.episodes.length, 0);
    const plural = (count, noun) => `${count} ${noun}${count === 1 ? "" : "s"}`;
    detailEpisodesSummary.textContent = regular.length
      ? `${plural(regular.length, "season")} · ${plural(episodeCount, "episode")}`
      : "";
    detailSeasons.replaceChildren(
      ...seasons.map((season) => {
        const row = document.createElement("section");
        row.className = "season-row";
        const head = document.createElement("header");
        head.className = "season-row-head";
        const title = document.createElement("h4");
        title.id = `season-${season.season_number}-title`;
        // The first season also says how many there are ("Season 1 of 7"); custom names stay.
        const counted = season === regular[0] && /^Season \d+$/.test(season.name);
        title.textContent = counted ? `${season.name} of ${regular.length}` : season.name;
        row.setAttribute("aria-labelledby", title.id);
        const meta = document.createElement("span");
        const local = season.episodes.filter((episode) => episode.in_library).length;
        meta.textContent = [
          plural(season.episodes.length, "episode"),
          local ? `${local} in library` : "",
        ].filter(Boolean).join(" · ");
        head.append(title, meta);
        const strip = document.createElement("ol");
        strip.className = "episode-strip";
        strip.append(...season.episodes.map((episode) => renderEpisode(episode, season.season_number, today)));
        row.append(head, strip);
        return row;
      }),
    );
    detailEpisodesStatus.textContent = seasons.length ? "" : "No episode list is available yet.";
    // A service link opens the show itself, so it starts from the first aired episode.
    const first = regular.find((season) => season.episodes.some((episode) => episode.air_date && episode.air_date <= today));
    const firstEpisode = first?.episodes.find((episode) => episode.air_date && episode.air_date <= today);
    streamPlayLabel = firstEpisode ? `Play S${first.season_number} E${firstEpisode.episode_number}` : "Play";
    return firstEpisode ? { season: first.season_number, episode: firstEpisode.episode_number } : null;
  };

  const renderRuntime = () => {
    detailRuntime.textContent = runtimeLabel(episodeRuntime ?? showRuntime);
  };

  const runtimeLabel = (minutes) => {
    if (!Number.isInteger(minutes) || minutes <= 0) {
      return "";
    }
    const hours = Math.floor(minutes / 60);
    const remainder = minutes % 60;
    return hours ? `${hours}h${remainder ? ` ${remainder}m` : ""}` : `${minutes}m`;
  };

  // The summary names the first three; it expands into the full cast with portraits, and stays
  // expanded while stepping through titles until the dialog closes.
  let castExpanded = false;

  const setCastExpanded = (expanded) => {
    castExpanded = expanded;
    detailCast.setAttribute("aria-expanded", String(expanded));
    detailCast.title = expanded ? "Hide cast" : "Show full cast";
    detailCastList.hidden = !expanded || detailCast.hidden;
  };

  const castPerson = (person) => {
    const item = document.createElement("article");
    item.className = "cast-person";
    item.setAttribute("role", "listitem");
    const portrait = document.createElement("div");
    portrait.className = "cast-person-image";
    if (person.profile_url) {
      const image = document.createElement("img");
      image.src = person.profile_url;
      image.alt = "";
      image.loading = "lazy";
      portrait.append(image);
    } else {
      portrait.textContent = person.name?.charAt(0) || "?";
    }
    const name = document.createElement("strong");
    name.textContent = person.name || "";
    item.append(portrait, name);
    if (person.character) {
      const character = document.createElement("small");
      character.textContent = person.character;
      item.append(character);
    }
    return item;
  };

  const renderCast = (people) => {
    const cast = people.filter((person) => person.name);
    const names = cast.map((person) => person.name);
    detailCast.hidden = names.length === 0;
    detailCastNames.textContent = names.slice(0, 3).join(", ") + (names.length > 3 ? ", …" : "");
    detailCastList.replaceChildren(...cast.map(castPerson));
    setCastExpanded(castExpanded);
  };

  const renderDetails = (payload) => {
    detailTitle.textContent = payload.title || detailTitle.textContent;
    setKind(payload.years || payload.year, payload.media_label);
    const directedBy = Array.isArray(payload.directed_by) ? payload.directed_by : [];
    setCredit(payload.media_type === "tv" ? "Created by" : "Directed by", directedBy.join(", "));
    showRuntime = payload.runtime_minutes;
    renderRuntime();
    detailRating.textContent = formatRating(payload.rating);
    // The card's cover is usually the same image; swapping it would flash mid-flight.
    if (payload.poster_url && detailPoster.querySelector("img")?.src !== payload.poster_url) {
      const poster = document.createElement("img");
      poster.src = payload.poster_url;
      poster.alt = "";
      detailPoster.replaceChildren(poster);
    }

    renderGenres(Array.isArray(payload.genres) ? payload.genres : []);
    detailOverview.textContent = payload.overview || "No description is available yet.";
    renderCast(Array.isArray(payload.cast) ? payload.cast : []);

    if (payload.backdrop_url) {
      detailBackdrop.onload = () => detailBackdrop.classList.add("has-image");
      detailBackdrop.src = payload.backdrop_url;
      if (detailBackdrop.complete) {
        detailBackdrop.classList.add("has-image");
      }
    }

    const autoplayTrailer = document.body.dataset.autoplayTrailer !== "false";
    if (payload.trailer_key && autoplayTrailer && !reducedMotion.matches) {
      const key = encodeURIComponent(payload.trailer_key);
      const playerParams = new URLSearchParams({
        autoplay: "1",
        mute: "1",
        controls: "0",
        loop: "1",
        playlist: payload.trailer_key,
        playsinline: "1",
        rel: "0",
        modestbranding: "1",
        enablejsapi: "1",
        origin: window.location.origin,
      });
      const iframe = document.createElement("iframe");
      iframe.src = `https://www.youtube-nocookie.com/embed/${key}?${playerParams}`;
      iframe.title = `${payload.title || "Media"} trailer`;
      iframe.tabIndex = -1;
      iframe.loading = "eager";
      iframe.allow = "autoplay; encrypted-media; picture-in-picture";
      iframe.referrerPolicy = "strict-origin-when-cross-origin";
      iframe.addEventListener("load", () => detailVideo.classList.add("is-active"));
      detailVideo.replaceChildren(iframe);
      setTrailerMuted(true);
      detailSound.hidden = false;
    }
    if (payload.trailer_url) {
      detailTrailer.href = payload.trailer_url;
      detailTrailer.hidden = false;
    }
    detailStatus.textContent = "";
    detailContent.hidden = false;
  };

  const fetchDetails = (url) => {
    if (!detailCache.has(url)) {
      const request = fetch(url, { headers: { Accept: "application/json" } })
        .then(async (response) => {
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.error || "Details unavailable");
          }
          return payload;
        })
        .catch((error) => {
          detailCache.delete(url);
          throw error;
        });
      detailCache.set(url, request);
    }
    return detailCache.get(url);
  };

  const loadDetails = async (card) => {
    const revision = ++detailRevision;
    if (!card.dataset.detailUrl) {
      detailStatus.textContent = "This title is not matched on TMDB yet, so extra details are unavailable.";
      return;
    }

    const itemUrl = card.dataset.detailUrl.replace(/\/details$/, "");
    const isCurrent = () => revision === detailRevision && detailDialog.open;
    fetchDetails(`${itemUrl}/watch`)
      .then((payload) => {
        if (isCurrent() && Array.isArray(payload.options)) {
          renderWatchOptions(payload.options);
        }
      })
      .catch(() => {
        // Watch links are optional; the details stay usable without them.
      });
    if (detailDialog.classList.contains("is-series")) {
      fetchDetails(`${itemUrl}/episodes`)
        .then((payload) => {
          if (isCurrent()) {
            const seasons = Array.isArray(payload.seasons) ? payload.seasons : [];
            const firstAired = renderSeasons(seasons);
            const target = payload.next_up?.url ? payload.next_up : firstAired;
            const episode = seasons
              .find((season) => season.season_number === target?.season)
              ?.episodes.find((candidate) => candidate.episode_number === target?.episode);
            if (episode?.runtime_minutes > 0) {
              episodeRuntime = episode.runtime_minutes;
              renderRuntime();
            }
            if (payload.next_up?.url) {
              localPlay = {
                url: payload.next_up.url,
                label: `${payload.next_up.resume ? "Resume" : "Play"} ${payload.next_up.label.replace(" · ", " ")}`,
              };
            }
            renderPlay();
          }
        })
        .catch(() => {
          if (isCurrent()) {
            detailEpisodesStatus.textContent = "Episodes are unavailable right now.";
          }
        });
    }

    try {
      const payload = await fetchDetails(card.dataset.detailUrl);
      if (isCurrent()) {
        renderDetails(payload);
      }
    } catch {
      if (isCurrent()) {
        detailStatus.textContent = "Extra details are unavailable right now.";
      }
    }
  };

  // The titles before and after this one on the page peek in from the edges.
  const neighborOf = (step) => detailCards[detailCards.indexOf(activeDetailCard) + step];

  const renderNeighbors = () => {
    detailNeighbors.forEach((button) => {
      const step = Number(button.dataset.detailNeighbor);
      const card = neighborOf(step);
      button.hidden = !card;
      button.replaceChildren();
      if (card) {
        button.append(posterVisual(card));
        const label = `${step < 0 ? "Previous" : "Next"}: ${card.dataset.title}`;
        button.setAttribute("aria-label", label);
        button.title = label;
      }
    });
  };

  // Save menus list the collections with a checkbox each, to add the title or take it out.
  const viewedCollectionId = Number(document.querySelector("[data-collection-id]")?.dataset.collectionId) || null;
  let viewedCollectionChanged = false;
  const savedTitleOf = (card) => card.dataset.detailUrl.replace(/^\/api\/items\//, "").replace(/\/details$/, "");

  const iconSet = document.querySelector("#collection-icon-set")?.content;
  const collectionIcon = (name) => {
    const icon = name && iconSet?.querySelector(`[data-icon="${CSS.escape(name)}"] svg`);
    if (icon) return icon.cloneNode(true);
    const blank = document.createElement("span");
    blank.className = "collection-icon";
    return blank;
  };

  // Once saved, a + turns into the icons of the collections holding the title.
  const savedCheck = '<svg class="collection-icon" aria-hidden="true" viewBox="0 0 24 24"><path d="m6 12.5 4 4 8-9"></path></svg>';
  const renderSavedIcons = (target, collections) => {
    const icons = [...new Set(collections.filter((collection) => collection.saved).map((collection) => collection.icon || ""))];
    target.replaceChildren(
      ...icons.map((icon) => (icon ? collectionIcon(icon) : document.createRange().createContextualFragment(savedCheck))),
    );
  };

  // The + on every card of the title (a title can sit in several rows) changes with it.
  const markCardsSaved = (title, collections) => {
    const saved = collections.some((collection) => collection.saved);
    document.querySelectorAll(`.media-card[data-detail-url="/api/items/${title}/details"]`).forEach((card) => {
      const button = card.querySelector("[data-card-save]");
      if (!button) return;
      button.classList.toggle("is-saved", saved);
      button.setAttribute("aria-label", `${saved ? "Saved" : "Save"} ${card.dataset.title} to collections`);
      button.title = saved ? "In your collections" : "Add to a collection";
      renderSavedIcons(button.querySelector("[data-saved-icons]"), collections);
    });
  };

  const createSaveMenu = (root, { onRender, beforeNew } = {}) => {
    const list = root.querySelector("[data-save-list]");
    const error = root.querySelector("[data-save-error]");
    const menu = { title: null, collections: [] };

    const render = () => {
      // Re-rendering replaces the buttons, so the focused one hands focus to its replacement.
      const focused = Array.from(list.children).indexOf(document.activeElement);
      // Names stay aligned when only some collections have an icon.
      const anyIcon = menu.collections.some((collection) => collection.icon);
      list.replaceChildren(
        ...menu.collections.map((collection) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "save-choice";
          button.setAttribute("aria-pressed", String(collection.saved));
          button.innerHTML = '<span class="save-check" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m5.5 12.5 4.2 4.2 8.8-9.4"></path></svg></span>';
          if (anyIcon) button.append(collectionIcon(collection.icon));
          const name = document.createElement("span");
          name.textContent = collection.name;
          button.append(name);
          button.addEventListener("click", () => toggle(collection));
          return button;
        }),
      );
      if (focused >= 0) list.children[focused]?.focus();
      if (menu.collections.length) markCardsSaved(menu.title, menu.collections);
      onRender?.(menu.collections);
    };

    const toggle = async (collection) => {
      const title = menu.title;
      const saved = !collection.saved;
      collection.saved = saved;
      error.hidden = true;
      render();
      try {
        const response = await fetch(`/api/collections/${collection.id}/items/${title}`, {
          method: saved ? "PUT" : "DELETE",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error();
        if (collection.id === viewedCollectionId) viewedCollectionChanged = true;
      } catch {
        collection.saved = !saved;
        if (title === menu.title) {
          error.textContent = `Could not update “${collection.name}”. Try again.`;
          error.hidden = false;
          render();
        }
      }
    };

    menu.load = async (title) => {
      error.hidden = true;
      menu.title = title;
      menu.collections = [];
      render();
      try {
        const response = await fetch(`/api/items/${title}/collections`, { headers: { Accept: "application/json" } });
        const payload = await response.json();
        if (title === menu.title) {
          menu.collections = payload.collections;
          render();
        }
      } catch {
        // The menu stays empty; saving can be retried after reopening it.
      }
    };

    root.querySelector("[data-save-new]")?.addEventListener("click", () => {
      beforeNew?.();
      const title = menu.title;
      openCollectionEditor({
        onSaved: async (collection) => {
          const entry = { ...collection, saved: false };
          menu.collections.push(entry);
          if (title === menu.title) await toggle(entry);
        },
      });
    });
    return menu;
  };

  // Save in title details.
  const saveButton = detailDialog?.querySelector("[data-detail-save]");
  const detailSaveMenu =
    saveButton &&
    createSaveMenu(detailDialog.querySelector("#save-popover"), {
      onRender: (collections) => {
        const saved = collections.filter((collection) => collection.saved);
        saveButton.classList.toggle("is-saved", saved.length > 0);
        renderSavedIcons(saveButton.querySelector("[data-saved-icons]"), collections);
        saveButton.querySelector("[data-detail-save-label]").textContent = saved.length ? "Saved" : "Save";
        saveButton.setAttribute(
          "aria-label",
          saved.length ? `Saved in ${saved.map((collection) => collection.name).join(", ")}` : "Save to a collection",
        );
      },
      beforeNew: () => closePopover(saveButton),
    });

  const loadSave = (card) => {
    if (!detailSaveMenu) return;
    closePopover(saveButton);
    detailSaveMenu.load(savedTitleOf(card));
  };

  detailDialog?.addEventListener("close", () => {
    if (viewedCollectionChanged) window.location.reload();
  });

  // The + on a card opens one shared Save menu beside it.
  const cardSave = document.querySelector("#card-save-popover");
  let cardSaveButton = null;

  const placeCardSave = () => {
    if (!cardSaveButton) return;
    const anchor = cardSaveButton.getBoundingClientRect();
    const width = cardSave.offsetWidth;
    const height = cardSave.offsetHeight;
    const left = Math.min(Math.max(8, anchor.right - width), window.innerWidth - width - 8);
    const below = anchor.bottom + 6;
    const top = below + height > window.innerHeight - 8 && anchor.top - 6 - height > 8 ? anchor.top - 6 - height : below;
    cardSave.style.left = `${left}px`;
    cardSave.style.top = `${Math.max(8, top)}px`;
  };

  const closeCardSave = ({ restoreFocus = false, reload = true } = {}) => {
    if (!cardSaveButton) return;
    const button = cardSaveButton;
    cardSaveButton = null;
    cardSave.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (restoreFocus) button.focus();
    if (reload && viewedCollectionChanged) window.location.reload();
  };

  const cardSaveMenu =
    cardSave &&
    createSaveMenu(cardSave, {
      onRender: placeCardSave,
      beforeNew: () => closeCardSave({ reload: false }),
    });

  if (cardSaveMenu) {
    document.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-card-save]");
      if (button) {
        const opening = button !== cardSaveButton;
        closeCardSave();
        if (!opening) return;
        cardSaveButton = button;
        button.setAttribute("aria-expanded", "true");
        cardSave.hidden = false;
        cardSaveMenu.load(savedTitleOf(button.closest(".media-card"))).then(() => {
          if (cardSaveButton === button) cardSave.querySelector("button")?.focus();
        });
      } else if (cardSaveButton && !event.composedPath().includes(cardSave)) {
        closeCardSave();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && cardSaveButton) {
        event.preventDefault();
        closeCardSave({ restoreFocus: true });
      }
    });
    cardSave.addEventListener("focusout", (event) => {
      const next = event.relatedTarget;
      if (next && next !== cardSaveButton && !cardSave.contains(next)) closeCardSave();
    });
    document.addEventListener("scroll", placeCardSave, { capture: true, passive: true });
    window.addEventListener("resize", placeCardSave);
  }

  const openMediaDetails = (card) => {
    if (!detailDialog || detailDialog.open) {
      return;
    }
    activeDetailCard = card;
    castExpanded = false;
    const motionRevision = ++posterMotionRevision;
    const sourceRect = card.querySelector(".poster").getBoundingClientRect();
    resetDetailContent(card);
    renderNeighbors();
    detailDialog.classList.remove("is-closing", "is-visible", "is-poster-fading");
    detailDialog.showModal();
    detailDialog.scrollTop = 0;
    document.body.classList.add("media-details-open");
    card.classList.add("is-detail-source");
    // flyPoster measures layout, so the hidden starting styles are applied before is-visible.
    const flight = flyPoster(sourceRect);
    detailDialog.classList.add("is-visible");
    settleFlight(flight, motionRevision);
    loadDetails(card);
    loadSave(card);
  };

  // Stepping to a neighbor flies its peeking cover into place, while the cover being left slides
  // out to the opposite edge and the rest of the page re-enters from the side it came from.
  const showNeighbor = (step) => {
    const card = neighborOf(step);
    if (!card || !detailDialog.open || detailDialog.classList.contains("is-closing")) {
      return;
    }
    detailDialog.scrollTop = 0;
    const button = detailNeighbors.find((candidate) => Number(candidate.dataset.detailNeighbor) === step);
    const arrivingRect = button.getBoundingClientRect();
    const leavingRect = detailPoster.getBoundingClientRect();
    activeDetailCard.classList.remove("is-detail-source");
    activeDetailCard = card;
    card.classList.add("is-detail-source");
    // Keep the card in view behind the dialog so closing can fly the cover back to it.
    card.scrollIntoView({ block: "nearest", behavior: "instant" });
    const motionRevision = ++posterMotionRevision;
    resetDetailContent(card);
    renderNeighbors();
    settleFlight(flyPoster(arrivingRect), motionRevision);
    if (!reducedMotion.matches && "animate" in Element.prototype) {
      const opposite = detailNeighbors.find((candidate) => Number(candidate.dataset.detailNeighbor) === -step);
      const oppositeRect = opposite.hidden ? null : opposite.getBoundingClientRect();
      if (oppositeRect?.width) {
        opposite.animate(
          [
            {
              transform: `translate(${leavingRect.left - oppositeRect.left}px, ${leavingRect.top - oppositeRect.top}px) scale(${leavingRect.width / oppositeRect.width}, ${leavingRect.height / oppositeRect.height})`,
            },
            { transform: "none" },
          ],
          { duration: 560, easing: "cubic-bezier(0.16, 1, 0.3, 1)" },
        );
      }
      button.animate(
        [{ opacity: 0, transform: `translateX(${step * 40}px)` }, { opacity: 1, transform: "none" }],
        { duration: 420, delay: 120, easing: "ease-out", fill: "backwards" },
      );
      detailDialog
        .querySelectorAll(
          ".media-detail-hero, .media-detail-identity > :not(.media-detail-poster), .media-detail-stage, .media-detail-episodes",
        )
        .forEach((element) => {
          element.animate(
            [{ opacity: 0, transform: `translateX(${step * 28}px)` }, { opacity: 1, transform: "none" }],
            { duration: 460, delay: 80, easing: "cubic-bezier(0.22, 1, 0.36, 1)", fill: "backwards" },
          );
        });
    }
    loadDetails(card);
    loadSave(card);
  };

  const closeMediaDetails = async () => {
    if (!detailDialog?.open || detailDialog.classList.contains("is-closing")) {
      return;
    }
    ++detailRevision;
    const motionRevision = ++posterMotionRevision;
    detailVideo.replaceChildren();
    detailVideo.classList.remove("is-active");
    detailSound.hidden = true;
    const card = activeDetailCard;
    const destinationRect = card?.querySelector(".poster")?.getBoundingClientRect();
    const destinationVisible =
      destinationRect && destinationRect.bottom > 0 && destinationRect.top < window.innerHeight;
    detailDialog.classList.add("is-closing");
    detailDialog.classList.remove("is-visible");
    const flight = destinationVisible ? flyPoster(destinationRect, { reverse: true }) : null;
    if (flight) {
      try {
        await flight.finished;
      } catch {
        // Superseded by another open or close.
      }
    } else {
      detailDialog.classList.add("is-poster-fading");
      await new Promise((resolve) => window.setTimeout(resolve, 340));
    }
    if (motionRevision !== posterMotionRevision) {
      return;
    }
    detailDialog.close();
    flight?.cancel();
    detailDialog.classList.remove("is-closing", "is-poster-fading");
    card?.classList.remove("is-detail-source");
    document.body.classList.remove("media-details-open");
    card?.querySelector("[data-open-media-details]")?.focus();
  };

  if (detailDialog) {
    document.querySelectorAll("[data-open-media-details]").forEach((button) => {
      button.addEventListener("click", () => openMediaDetails(button.closest(".media-card")));
    });
    detailDialog.querySelector("[data-close-media-details]")?.addEventListener("click", closeMediaDetails);
    detailNeighbors.forEach((button) => {
      button.addEventListener("click", () => showNeighbor(Number(button.dataset.detailNeighbor)));
    });
    detailCast.addEventListener("click", () => setCastExpanded(!castExpanded));
    detailSound?.addEventListener("click", () => {
      setTrailerMuted(detailSound.dataset.muted !== "true", true);
    });
    detailDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      if (!escapeClosedPopover(event)) closeMediaDetails();
    });
    detailDialog.addEventListener("click", (event) => {
      if (event.target === detailDialog) {
        closeMediaDetails();
      }
    });
    // Arrow keys step through titles, except where they already scroll a row of episodes.
    detailDialog.addEventListener("keydown", (event) => {
      const step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
      if (!step || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) {
        return;
      }
      if (event.target.closest?.(".episode-strip, .media-detail-cast-list, .popover, input, textarea, select")) {
        return;
      }
      event.preventDefault();
      showNeighbor(step);
    });
    let swipeStart = null;
    detailDialog.addEventListener(
      "touchstart",
      (event) => {
        const touch = event.touches[0];
        swipeStart =
          event.touches.length === 1 && !event.target.closest(".episode-strip, .media-detail-cast-list")
            ? { x: touch.clientX, y: touch.clientY }
            : null;
      },
      { passive: true },
    );
    detailDialog.addEventListener("touchend", (event) => {
      const touch = event.changedTouches[0];
      if (!swipeStart || !touch) {
        return;
      }
      const dx = touch.clientX - swipeStart.x;
      const dy = touch.clientY - swipeStart.y;
      swipeStart = null;
      if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.8) {
        showNeighbor(dx < 0 ? 1 : -1);
      }
    });
  }

  const peopleCards = Array.from(document.querySelectorAll("[data-people-url]"));
  let peopleObserver;

  const loadPeople = async (element) => {
    if (element.dataset.loaded === "true" || element.dataset.loading === "true") {
      return;
    }
    element.dataset.loading = "true";
    try {
      const response = await fetch(element.dataset.peopleUrl, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error("Credits unavailable");
      }
      const payload = await response.json();
      if (Array.isArray(payload.names) && payload.names.length) {
        element.textContent = payload.names.join(", ");
      } else {
        element.textContent = "";
      }
      element.dataset.loaded = "true";
    } catch {
      element.textContent = "";
    } finally {
      delete element.dataset.loading;
    }
  };

  const observePeople = () => {
    if (document.body.dataset.showPeople !== "true") {
      peopleObserver?.disconnect();
      return;
    }
    if (!("IntersectionObserver" in window)) {
      peopleCards.forEach(loadPeople);
      return;
    }
    if (!peopleObserver) {
      peopleObserver = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              loadPeople(entry.target);
              peopleObserver.unobserve(entry.target);
            }
          });
        },
        { rootMargin: "180px" },
      );
    }
    peopleCards
      .filter((element) => element.dataset.loaded !== "true")
      .forEach((element) => peopleObserver.observe(element));
  };

  const providerStrips = Array.from(document.querySelectorAll("[data-providers-key]"));
  const providerQueue = new Set();
  let providerTimer;
  let providerObserver;

  const renderProviders = (strip, providers) => {
    const local = strip.querySelector(".source-local");
    const icons = providers.map((provider) => {
      if (provider.logo_url) {
        const logo = document.createElement("img");
        logo.src = provider.logo_url;
        logo.alt = "";
        logo.width = 24;
        logo.height = 24;
        logo.decoding = "async";
        return logo;
      }
      const letter = document.createElement("span");
      letter.textContent = provider.name.slice(0, 1);
      return letter;
    });
    strip.replaceChildren(...icons, ...(local ? [local] : []));
    const names = providers.map((provider) => provider.name).join(", ");
    const summary = [names && `On ${names}`, local && "In your local library"].filter(Boolean);
    strip.title = summary.join(" · ");
    const text = strip.closest(".media-card")?.querySelector("[data-providers-text]");
    if (text) {
      text.textContent = summary.join(". ");
    }
  };

  const flushProviders = async () => {
    providerTimer = undefined;
    const batch = Array.from(providerQueue);
    providerQueue.clear();
    // Search results from other services list every service that carries them.
    const keysFor = (scope) =>
      Array.from(
        new Set(
          batch
            .filter((strip) => (strip.dataset.providersScope || "providers") === scope)
            .map((strip) => strip.dataset.providersKey),
        ),
      );
    try {
      const params = new URLSearchParams({ items: keysFor("providers").join(","), any: keysFor("any").join(",") });
      const response = await fetch(`/api/items/providers?${params}`, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error("Services unavailable");
      }
      const payload = await response.json();
      batch.forEach((strip) => {
        const providers = payload[strip.dataset.providersScope || "providers"]?.[strip.dataset.providersKey];
        if (providers) {
          renderProviders(strip, providers);
          strip.dataset.loaded = "true";
        }
      });
    } catch {
      // Leave the strips empty; they are retried the next time the option is enabled.
    } finally {
      batch.forEach((strip) => delete strip.dataset.loading);
    }
  };

  const queueProviders = (strip) => {
    if (strip.dataset.loaded === "true" || strip.dataset.loading === "true") {
      return;
    }
    strip.dataset.loading = "true";
    providerQueue.add(strip);
    providerTimer ??= window.setTimeout(flushProviders, 40);
  };

  const observeProviders = () => {
    if (document.body.dataset.showProviders !== "true") {
      providerObserver?.disconnect();
      return;
    }
    if (!("IntersectionObserver" in window)) {
      providerStrips.forEach(queueProviders);
      return;
    }
    providerObserver ??= new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            queueProviders(entry.target);
            providerObserver.unobserve(entry.target);
          }
        });
      },
      { rootMargin: "240px" },
    );
    providerStrips
      .filter((strip) => strip.dataset.loaded !== "true")
      .forEach((strip) => providerObserver.observe(strip));
  };

  const displayForm = document.querySelector("#display-form");
  const displayStatus = document.querySelector("#display-save-status");
  let displaySaveQueue = Promise.resolve();
  let displaySaveRevision = 0;
  const displayKeys = [
    "show_year",
    "show_rating",
    "show_media_type",
    "show_genres",
    "show_people",
    "show_providers",
    "autoplay_trailer",
  ];

  const displayPayload = () => {
    const data = new FormData(displayForm);
    return {
      ...Object.fromEntries(
        displayKeys.filter((key) => displayForm.elements[key]).map((key) => [key, data.has(key)]),
      ),
      card_size: data.get("card_size") || "comfortable",
    };
  };

  const applyDisplay = (payload) => {
    displayKeys.filter((key) => key in payload).forEach((key) => {
      const dataKey = key.replace(/_([a-z])/g, (_, value) => value.toUpperCase());
      document.body.dataset[dataKey] = String(payload[key]);
    });
    document.body.dataset.cardSize = payload.card_size;
    if (!payload.autoplay_trailer) {
      detailVideo?.replaceChildren();
      detailVideo?.classList.remove("is-active");
      if (detailSound) {
        detailSound.hidden = true;
      }
    }
    observePeople();
    observeProviders();
  };

  const saveDisplay = (payload) => {
    const revision = ++displaySaveRevision;
    if (displayStatus) {
      displayStatus.textContent = "Saving…";
    }
    displaySaveQueue = displaySaveQueue.then(async () => {
      try {
        const response = await fetch("/api/preferences/display", {
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          throw new Error("Could not save display settings");
        }
        if (displayStatus && revision === displaySaveRevision) {
          displayStatus.textContent = "Saved automatically";
        }
      } catch {
        if (displayStatus && revision === displaySaveRevision) {
          displayStatus.textContent = "Could not save. Try changing the option again.";
        }
      }
    });
  };

  if (displayForm) {
    displayForm.addEventListener("change", () => {
      const payload = displayPayload();
      applyDisplay(payload);
      saveDisplay(payload);
    });
  }

  observePeople();
  observeProviders();

  // Infinite scroll: near the bottom, the next page's cards (or rows, when grouped) are fetched
  // and appended. The page links stay in the markup for browsers without JavaScript.
  const enhanceCards = (cards) => {
    cards.forEach((card) => {
      detailCards.push(card);
      card.querySelectorAll("[data-open-media-details]").forEach((button) => {
        button.addEventListener("click", () => openMediaDetails(card));
      });
      peopleCards.push(...card.querySelectorAll("[data-people-url]"));
      providerStrips.push(...card.querySelectorAll("[data-providers-key]"));
    });
    observePeople();
    observeProviders();
  };

  const initializeInfiniteScroll = () => {
    const results = document.querySelector("[data-results]");
    const loadMore = document.querySelector("[data-load-more]");
    const pagination = document.querySelector("[data-pagination]");
    let nextPage = pagination?.querySelector("[data-next-page]")?.href ?? null;

    if (results?.dataset.scrollReady) return;

    if (results && loadMore && pagination && nextPage && "IntersectionObserver" in window) {
      results.dataset.scrollReady = "true";
      const status = loadMore.querySelector("[data-load-more-status]");
      const retry = loadMore.querySelector("[data-load-more-retry]");
      const reach = 1200;
      let loading = false;
      let observer;

      const nearBottom = () => loadMore.getBoundingClientRect().top < window.innerHeight + reach;

      const fetchNext = async () => {
        if (loading || !nextPage) return;
        loading = true;
        retry.hidden = true;
        status.textContent = "Loading more…";
        // A grid shows two rows of skeleton cards; grouped rows keep the text.
        const placeholders = results.matches(".media-grid") ? skeletonCards(columnsOf(results) * 2) : [];
        results.append(...placeholders);
        status.classList.toggle("sr-only", placeholders.length > 0);
        let loaded = false;
        try {
          const response = await fetch(nextPage, { headers: { "X-MyTaste-Fragment": "results" } });
          if (!response.ok) throw new Error("More titles are unavailable");
          const batch = document.createElement("template");
          batch.innerHTML = await response.text();
          // Pages can overlap (search results shift between requests), so a title already in the
          // grid is not added twice. Grouped rows repeat titles on purpose and are kept whole.
          const shown = new Set(
            Array.from(results.children, (element) => element.dataset.detailUrl).filter(Boolean),
          );
          const added = Array.from(batch.content.children).filter(
            (element) => !element.dataset.detailUrl || !shown.has(element.dataset.detailUrl),
          );
          placeholders.forEach((card) => card.remove());
          results.append(...added);
          enhanceCards(
            added.flatMap((element) =>
              element.matches(".media-card") ? [element] : Array.from(element.querySelectorAll(".media-card")),
            ),
          );
          nextPage = response.headers.get("X-Next-Page") || null;
          status.textContent = nextPage ? "" : "That’s everything.";
          loaded = true;
        } catch {
          status.textContent = "More titles could not be loaded.";
          retry.hidden = false;
        } finally {
          loading = false;
          placeholders.forEach((card) => card.remove());
          status.classList.remove("sr-only");
        }
        if (!nextPage) {
          observer.disconnect();
        } else if (loaded && nearBottom()) {
          // A short batch can leave the end of the list on screen; keep filling.
          fetchNext();
        }
      };

      pagination.hidden = true;
      loadMore.hidden = false;
      observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) fetchNext();
      }, { rootMargin: `${reach}px 0px` });
      observer.observe(loadMore);
      retry.addEventListener("click", fetchNext);
    }
  };
  initializeInfiniteScroll();
  document.addEventListener("mytaste:results-ready", initializeInfiniteScroll);

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
      }
    });
  });

  const libraryRows = Array.from(document.querySelectorAll("[data-library-id]"));
  let libraryPollTimer;

  const applyLibraryStatus = (payload) => {
    const byId = new Map(payload.map((library) => [String(library.id), library]));
    let scanning = false;
    libraryRows.forEach((row) => {
      const library = byId.get(row.dataset.libraryId);
      if (!library) {
        return;
      }
      row.dataset.state = library.state;
      const status = row.querySelector("[data-library-status]");
      if (status && status.textContent !== library.text) {
        status.textContent = library.text;
      }
      const rescan = row.querySelector("[data-library-rescan]");
      if (rescan) {
        rescan.disabled = library.state === "scanning";
      }
      scanning = scanning || library.state === "scanning";
    });
    return scanning;
  };

  const pollLibraries = async () => {
    try {
      const response = await fetch("/api/libraries/status", { headers: { Accept: "application/json" } });
      if (!response.ok) {
        throw new Error("Status unavailable");
      }
      const payload = await response.json();
      if (applyLibraryStatus(payload.libraries || [])) {
        libraryPollTimer = window.setTimeout(pollLibraries, 1500);
      }
    } catch {
      libraryPollTimer = window.setTimeout(pollLibraries, 5000);
    }
  };

  if (libraryRows.some((row) => row.dataset.state === "scanning")) {
    pollLibraries();
  }
  document.querySelectorAll("[data-library-rescan]").forEach((button) => {
    button.addEventListener("click", () => window.clearTimeout(libraryPollTimer));
  });

  // Library names are edited in place: the card swaps its title and actions for a small form.
  document.querySelectorAll("[data-library-rename]").forEach((button) => {
    const card = button.closest("[data-library-id]");
    const form = card.querySelector("[data-library-rename-form]");
    const input = form.querySelector("input");
    const saved = input.value;

    const setEditing = (editing) => {
      card.classList.toggle("is-renaming", editing);
      form.hidden = !editing;
      if (editing) {
        input.focus();
        input.select();
      } else {
        input.value = saved;
        button.focus();
      }
    };

    button.addEventListener("click", () => setEditing(true));
    form.querySelector("[data-library-rename-cancel]").addEventListener("click", () => setEditing(false));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setEditing(false);
      }
    });
  });

  const addDialog = document.querySelector("#add-source");
  const libraryForm = document.querySelector("[data-library-form]");
  const nameInput = document.querySelector("#library-name");
  const folderPicker = document.querySelector("[data-folder-picker]");
  let openFolderPicker = () => {};
  let firstPathInput = () => null;
  let setLibraryTarget = () => {};

  if (libraryForm && folderPicker) {
    const folderRows = libraryForm.querySelector("[data-folder-rows]");
    const rowTemplate = libraryForm.querySelector("[data-folder-row-template]");
    const addRowButton = libraryForm.querySelector("[data-folder-add-row]");
    const nameField = libraryForm.querySelector("[data-library-name-field]");
    const librarySubmit = libraryForm.querySelector("[data-library-submit]");
    const localPanel = libraryForm.closest("[data-add-panel]");
    const folderEntries = folderPicker.querySelector("[data-folder-entries]");
    const folderCurrent = folderPicker.querySelector("[data-folder-current]");
    const folderStatus = folderPicker.querySelector("[data-folder-status]");
    const folderUp = folderPicker.querySelector("[data-folder-up]");
    const folderChoose = folderPicker.querySelector("[data-folder-choose]");
    let currentListing = { path: "", parent: null, entries: [] };
    let activeRow = null;
    let suggestedName = nameInput?.value || "";
    let libraryTarget = libraryForm.getAttribute("action").match(/libraries\/(\d+)\/folders/)?.[1] || "";
    let startNear = "";

    const rows = () => Array.from(folderRows.querySelectorAll("[data-folder-row]"));
    const pathOf = (row) => row.querySelector("[data-folder-path]");
    const lastSegment = (path) => path.split("/").filter(Boolean).pop() || "";
    firstPathInput = () => pathOf(rows().find((row) => !pathOf(row).value.trim()) || rows()[0]);

    // A single folder names the library after itself; several folders fall back to "Local library".
    const updateSuggestedName = () => {
      if (!nameInput) {
        return;
      }
      const all = rows();
      const next = all.length === 1 ? lastSegment(pathOf(all[0]).value.trim()) : "";
      if (!nameInput.value || nameInput.value === suggestedName) {
        nameInput.value = next;
      }
      suggestedName = next;
      nameInput.placeholder = all.length === 1 ? "Uses the folder name" : "Local library";
    };

    // Radio groups are numbered by position so the server can pair each path with its type.
    const renumber = () => {
      const all = rows();
      all.forEach((row, index) => {
        row.querySelectorAll('input[type="radio"]').forEach((radio) => {
          radio.name = `media_type_${index}`;
        });
        pathOf(row).id = `library-path-${index}`;
        row.querySelector("[data-folder-remove-row]").hidden = all.length === 1;
      });
      updateSuggestedName();
    };

    const guessType = (row, path) => {
      if ("typeChosen" in row.dataset) {
        return;
      }
      const tv = /\b(tv|shows?|series)\b/i.test(lastSegment(path));
      row.querySelector(`input[type="radio"][value="${tv ? "tv" : "movie"}"]`).checked = true;
    };

    const usePath = (path) => {
      if (!activeRow) {
        return;
      }
      pathOf(activeRow).value = path;
      guessType(activeRow, path);
      updateSuggestedName();
    };

    const renderListing = (listing) => {
      currentListing = listing;
      folderCurrent.textContent = listing.path || "Allowed folders";
      folderUp.disabled = listing.parent === null;
      folderChoose.disabled = !listing.path;
      folderEntries.replaceChildren();
      listing.entries.forEach((entry) => {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        icon.setAttribute("viewBox", "0 0 24 24");
        icon.setAttribute("aria-hidden", "true");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute(
          "d",
          "M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l2 2h8.8A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-10Z",
        );
        icon.append(path);
        const label = document.createElement("span");
        label.textContent = entry.name;
        button.append(icon, label);
        button.addEventListener("click", () => loadFolder(entry.path, true));
        item.append(button);
        folderEntries.append(item);
      });
      folderStatus.textContent = listing.entries.length ? "" : "No subfolders here.";
    };

    const loadFolder = async (path, fill) => {
      folderStatus.textContent = "Loading…";
      try {
        const params = new URLSearchParams();
        if (path) {
          params.set("path", path);
        }
        const response = await fetch(`/api/libraries/folders?${params}`, {
          headers: { Accept: "application/json" },
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Folder unavailable");
        }
        renderListing(payload);
        if (fill && payload.path && payload.path !== "/") {
          usePath(payload.path);
        }
      } catch (error) {
        folderStatus.textContent = error.message || "Could not open that folder.";
      }
    };

    const closePicker = () => {
      folderPicker.hidden = true;
      folderRows.querySelectorAll("[data-folder-browse]").forEach((button) => {
        button.setAttribute("aria-expanded", "false");
      });
    };

    // The one folder picker moves under whichever row is being browsed. A new, empty row starts
    // next to the folder chosen in the row above it.
    const openPickerFor = (row) => {
      closePicker();
      activeRow = row;
      row.append(folderPicker);
      folderPicker.hidden = false;
      row.querySelector("[data-folder-browse]").setAttribute("aria-expanded", "true");
      let start = pathOf(row).value.trim();
      if (!start) {
        const above = rows()
          .slice(0, rows().indexOf(row))
          .map((candidate) => pathOf(candidate).value.trim())
          .filter(Boolean)
          .pop();
        const near = above || startNear;
        start = near ? near.replace(/\/[^/]+\/?$/, "") : "";
      }
      loadFolder(start, false);
    };

    openFolderPicker = () => {
      const all = rows();
      if (folderPicker.hidden && all.length === 1 && !pathOf(all[0]).value.trim()) {
        openPickerFor(all[0]);
      }
    };

    const addRow = () => {
      const fragment = rowTemplate.content.cloneNode(true);
      const row = fragment.querySelector("[data-folder-row]");
      folderRows.append(fragment);
      renumber();
      openPickerFor(row);
      pathOf(row).focus();
    };

    const removeRow = (row) => {
      const all = rows();
      const index = all.indexOf(row);
      if (row.contains(folderPicker)) {
        closePicker();
        folderRows.after(folderPicker);
        activeRow = null;
      }
      row.remove();
      renumber();
      const remaining = rows();
      pathOf(remaining[Math.min(index, remaining.length - 1)]).focus();
    };

    const resetRows = () => {
      closePicker();
      folderRows.after(folderPicker);
      activeRow = null;
      rows()
        .slice(1)
        .forEach((row) => row.remove());
      const [first] = rows();
      pathOf(first).value = "";
      delete first.dataset.typeChosen;
      first.querySelector('input[type="radio"][value="movie"]').checked = true;
      renumber();
    };

    // Opening the dialog for a different library (or a new one) starts from a single empty row.
    setLibraryTarget = (id, name, near) => {
      if (id !== libraryTarget) {
        libraryTarget = id;
        resetRows();
      }
      startNear = near || "";
      libraryForm.action = id ? `/settings/libraries/${id}/folders` : "/settings/libraries";
      nameField.hidden = Boolean(id);
      nameInput.disabled = Boolean(id);
      if (id) {
        nameInput.value = "";
        suggestedName = "";
      } else {
        updateSuggestedName();
      }
      localPanel.dataset.title = id ? `Add folders to ${name}` : "Add a local library";
      librarySubmit.textContent = id ? "Add and rescan" : "Add and scan";
    };

    folderRows.addEventListener("click", (event) => {
      const row = event.target.closest("[data-folder-row]");
      if (!row) {
        return;
      }
      if (event.target.closest("[data-folder-browse]")) {
        if (activeRow === row && !folderPicker.hidden) {
          closePicker();
        } else {
          openPickerFor(row);
        }
      } else if (event.target.closest("[data-folder-remove-row]")) {
        removeRow(row);
      }
    });
    folderRows.addEventListener("change", (event) => {
      if (event.target.matches('input[type="radio"]')) {
        event.target.closest("[data-folder-row]").dataset.typeChosen = "";
      }
    });
    folderRows.addEventListener("input", (event) => {
      if (event.target.matches("[data-folder-path]")) {
        updateSuggestedName();
      }
    });
    addRowButton.addEventListener("click", addRow);
    folderUp.addEventListener("click", () => {
      if (currentListing.parent !== null) {
        loadFolder(currentListing.parent, true);
      }
    });
    folderChoose.addEventListener("click", () => {
      const row = activeRow;
      if (currentListing.path) {
        usePath(currentListing.path);
      }
      closePicker();
      if (row) {
        pathOf(row).focus();
      }
    });
  }

  const providerForm = document.querySelector("[data-provider-form]");

  if (providerForm) {
    const providerSearch = providerForm.querySelector("[data-provider-search]");
    const providerChoices = Array.from(providerForm.querySelectorAll(".provider-choice"));
    const providerEmpty = providerForm.querySelector("[data-provider-empty]");
    const providerCount = providerForm.querySelector("[data-provider-count]");
    const providerSubmit = providerForm.querySelector("[data-provider-submit]");

    const updateProviderCount = () => {
      if (!providerChoices.length) {
        return;
      }
      const selected = providerChoices.filter((choice) => choice.querySelector("input").checked).length;
      providerSubmit.disabled = selected === 0;
      providerSubmit.textContent = selected ? `Add ${selected} service${selected === 1 ? "" : "s"}` : "Add services";
      providerCount.textContent = selected ? `${selected} selected` : "Select one or more";
    };

    providerSearch?.addEventListener("input", () => {
      const term = providerSearch.value.trim().toLowerCase();
      let visible = 0;
      providerChoices.forEach((choice) => {
        const match =
          !term || choice.dataset.providerName.includes(term) || choice.querySelector("input").checked;
        choice.hidden = !match;
        visible += match ? 1 : 0;
      });
      if (providerEmpty) {
        providerEmpty.hidden = visible > 0;
      }
    });
    providerSearch?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
      }
    });
    providerForm.addEventListener("change", updateProviderCount);
    updateProviderCount();
  }

  if (addDialog) {
    const addPanels = Array.from(addDialog.querySelectorAll("[data-add-panel]"));
    const addTitle = addDialog.querySelector("[data-add-title]");
    const addBack = addDialog.querySelector("[data-add-back]");
    const finePointer = window.matchMedia("(pointer: fine)");
    let addOpener;

    const focusStep = (step, panel) => {
      let target = panel?.querySelector(".source-choice");
      if (step === "streaming") {
        target = finePointer.matches ? panel.querySelector("[data-provider-search]") : null;
      } else if (step === "local") {
        target = finePointer.matches ? firstPathInput() : null;
      }
      (target || addTitle).focus();
    };

    const showStep = (step) => {
      const panel = addPanels.find((candidate) => candidate.dataset.addPanel === step) || addPanels[0];
      addPanels.forEach((candidate) => {
        candidate.hidden = candidate !== panel;
      });
      addTitle.textContent = panel.dataset.title;
      addBack.hidden = panel.dataset.addPanel === "choose";
      addDialog.dataset.step = panel.dataset.addPanel;
      if (panel.dataset.addPanel === "local") {
        openFolderPicker();
      }
      focusStep(panel.dataset.addPanel, panel);
    };

    const openAdd = (step, opener) => {
      addOpener = opener;
      if (!addDialog.open) {
        addDialog.showModal();
        document.body.classList.add("dialog-open");
      }
      showStep(step);
    };

    addTitle.tabIndex = -1;
    document.querySelectorAll("[data-open-add]").forEach((button) => {
      button.addEventListener("click", () => {
        setLibraryTarget(
          button.dataset.libraryTarget || "",
          button.dataset.libraryName || "",
          button.dataset.libraryNear || "",
        );
        openAdd(button.dataset.openAdd, button);
      });
    });
    addDialog.querySelectorAll("[data-add-go]").forEach((button) => {
      button.addEventListener("click", () => {
        setLibraryTarget("", "", "");
        showStep(button.dataset.addGo);
      });
    });
    addBack.addEventListener("click", () => showStep("choose"));
    addDialog.querySelector("[data-close-add]")?.addEventListener("click", () => addDialog.close());
    addDialog.addEventListener("click", (event) => {
      if (event.target === addDialog) {
        addDialog.close();
      }
    });
    addDialog.addEventListener("close", () => {
      document.body.classList.remove("dialog-open");
      addOpener?.focus();
    });

    if (addDialog.dataset.openStep) {
      openAdd(addDialog.dataset.openStep, document.querySelector("[data-open-add]"));
    }
  }

  const regionForm = document.querySelector("[data-region-form]");
  const regionSelect = regionForm?.querySelector("select");

  if (regionForm && regionSelect) {
    const savedRegion = regionSelect.value;
    regionSelect.addEventListener("change", () => {
      const enabled = document.querySelectorAll(".source-card:not(.source-card-local)").length;
      const name = regionSelect.selectedOptions[0]?.textContent.trim() || regionSelect.value;
      if (
        enabled &&
        !window.confirm(`Switch the streaming region to ${name}? Services that aren’t offered there are removed.`)
      ) {
        regionSelect.value = savedRegion;
        return;
      }
      regionForm.requestSubmit();
    });
  }
})();
