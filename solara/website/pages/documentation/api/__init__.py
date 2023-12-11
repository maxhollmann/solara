"""
# Overview
Click on one of the items on the left.
"""

from pathlib import Path

import solara
from solara.alias import rv

_title = "API"

HERE = Path(__file__).parent


@solara.component_vue(str(HERE.parent.parent.parent) + "/components/algolia_api.vue")
def Algolia():
    pass


items = [
    {
        "name": "Hooks",
        "icon": "mdi-hook",
        "pages": [
            "use_cross_filter",
            "use_thread",
            "use_exception",
            "use_effect",
            "use_memo",
            "use_previous",
            "use_reactive",
            "use_state",
            "use_state_or_update",
        ],
    },
    {
        "name": "Types",
        "icon": "mdi-fingerprint",
        "pages": ["route"],
    },
    {
        "name": "Routing",
        "icon": "mdi-router",
        "pages": ["use_route", "use_router", "resolve_path", "generate_routes", "generate_routes_directory", "link"],
    },
    {
        "name": "Utils",
        "icon": "mdi-hammer-wrench",
        "pages": ["display", "memoize", "reactive", "widget", "component_vue"],
    },
    {
        "name": "Cross filter",
        "icon": "mdi-filter-variant-remove",
        "pages": ["cross_filter_dataframe", "cross_filter_report", "cross_filter_slider", "cross_filter_select"],
    },
]


@solara.component
def Page(route_external=None):
    if route_external is not None:
        route_current = route_external
    else:
        # show a gallery of all the api pages
        router = solara.use_router()
        route_current = router.path_routes[-2]

    routes = {r.path: r for r in route_current.children}

    for item in items:
        solara.Markdown(f"## {item['name']}")
        with solara.Row(justify="center", gap="20px", style={"flex-wrap": "wrap", "row-gap": "20px"}):
            for page in item["pages"]:
                if page not in routes:
                    continue
                route = routes[page]
                path = route.path
                image_url = None
                if page in [
                    "button",
                    "checkbox",
                    "confirmation_dialog",
                    "echarts",
                    "file_browser",
                    "file_download",
                    "matplotlib",
                    "select",
                    "switch",
                    "tooltip",
                ]:
                    image_url = "https://dxhl76zpt6fap.cloudfront.net/public/api/" + page + ".gif"
                elif page in ["card", "dataframe", "pivot_table", "slider"]:
                    image_url = "https://dxhl76zpt6fap.cloudfront.net/public/api/" + page + ".png"
                else:
                    image_url = "https://dxhl76zpt6fap.cloudfront.net/public/logo.svg"

                path = getattr(route.module, "redirect", path)
                if path:
                    with solara.Card(classes=["component-card"], margin=0):
                        rv.CardTitle(children=[route.label])
                        with rv.CardText():
                            with solara.Link(path):
                                with solara.Column(align="center"):
                                    solara.Image(image_url, width="120px")
                        doc = route.module.__doc__ or ""
                        if doc:
                            lines = doc.split("\n")
                            lines = [line.strip() for line in lines if line.strip()]
                            first = lines[1]

                            rv.CardText(
                                children=[solara.Markdown(first)],
                            )


@solara.component
def NoPage():
    raise RuntimeError("This page should not be rendered")


@solara.component
def Sidebar(children=[], level=0):
    # note that we don't use children here, but we used route.module instead to ge the module
    # this is fine because all api/*.py files use the standard Page component, and do not add
    # a new Layout component
    route_current, all_routes = solara.use_route()
    if route_current is None:
        return solara.Error("Page not found")

    # keeps track of which routes we includes
    routes = {r.path: r for r in all_routes.copy()}

    def add(path):
        route = routes[path]
        with solara.Link(route):
            solara.v.ListItem(route.label, class_="active" if route_current is not None and path == route_current.path else None)
        del routes[path]

    # with solara.HBox(grow=True) as main:
    with rv.Col(tag="aside", md=4, lg=3, class_="sidebar bg-grey d-none d-md-block") as main:
        with solara.Head():
            name = route_current.label if route_current.label is not None else "No name"
            if name == "API":
                solara.Title("Solara » API overview")
            else:
                solara.Title("Solara » API » " + name)
        with solara.v.List():
            add("/")

            for item in items:
                with solara.v.ListItem(item["name"], icon_name=item["icon"]):
                    with solara.v.List():
                        for component in item["pages"]:
                            add(component)

        if routes:
            print(f"Routes not used: {list(routes.keys())}")  # noqa

    return main


@solara.component
def Layout(children=[]):
    route_current, all_routes = solara.use_route()
    if route_current is None:
        return solara.Error("Page not found")

    if route_current.path == "/":
        return Page()
    else:
        with solara.HBox(grow=True) as main:
            with solara.Padding(4):
                if route_current.module:
                    # we ignore children, and make the element again
                    WithCode(route_current.module)
        return main


@solara.component
def WithCode(module):
    # e = solara.use_exception_handler()
    # if e is not None:
    #     return solara.Error("oops")
    component = getattr(module, "Page", None)
    with rv.Sheet() as main:
        # It renders code better
        solara.Markdown(
            module.__doc__ or "# no docs yet",
            unsafe_solara_execute=True,
        )
        if component and component != NoPage:
            with solara.Card("Example", margin=0, classes=["mt-8"]):
                component()
                github_url = solara.util.github_url(module.__file__)
                solara.Button(
                    label="View source",
                    icon_name="mdi-github-circle",
                    attributes={"href": github_url, "target": "_blank"},
                    text=True,
                    outlined=True,
                    class_="mt-8",
                )
    return main
