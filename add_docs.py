""" doc generator
"""
import ast
import logging
import os
import shutil
import sys
from argparse import ArgumentParser
from functools import partial
from pathlib import Path


import yaml
from ansible.module_utils.common.collections import is_sequence
from ansible.utils import plugin_docs
from ansible.plugins.loader import fragment_loader
from ansible.module_utils.six import string_types
from jinja2 import Environment, FileSystemLoader
from jinja_utils import (
    to_kludge_ns,
    from_kludge_ns,
    html_ify,
    rst_ify,
    documented_type,
)


logging.basicConfig(format="%(levelname)-10s%(message)s", level=logging.INFO)


IGNORE_FILES = ["__init__.py"]
SUBDIRS = (
    "become",
    "cliconf",
    "connection",
    "filter",
    "httpapi",
    "netconf",
    "terminal",
    "modules",
)
TEMPLATE_DIR = "./"


def ensure_list(value):
    """ Ensure the value is a list

    :param value: The value to check
    :type value: Unknown
    :return: The value as a list
    """
    if isinstance(value, list):
        return value
    return [value]


def convert_descriptions(data):
    """ Convert the descriptions for doc into lists

    :param data: the chunk from the doc
    :type data: dict
    """
    if data:
        for definition in data.values():
            if "description" in definition:
                definition["description"] = ensure_list(definition["description"])
            if "suboptions" in definition:
                convert_descriptions(definition["suboptions"])
            if "contains" in definition:
                convert_descriptions(definition["contains"])


def jinja_environment():
    """ Define the jinja enviroment

    :return: A jinja template, with the env set
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        variable_start_string="@{",
        variable_end_string="}@",
        trim_blocks=True,
    )
    env.filters["rst_ify"] = rst_ify
    env.filters["documented_type"] = documented_type
    env.tests["list"] = partial(is_sequence, include_strings=False)
    env.filters["html_ify"] = html_ify
    env.globals["to_kludge_ns"] = to_kludge_ns
    env.globals["from_kludge_ns"] = from_kludge_ns
    template = env.get_template("plugin.rst.j2")
    return template


def update_readme(content, path, gh_url):
    """ Update the README.md in the repository

    :param content: The dict containing the content
    :type content: dict
    :param collection: The full collection name
    :type collection: str
    :param path: The path to the collection
    :type path: str
    :param gh_url: The url to the gihub repository
    :type gh_url: str
    """
    data = []
    for plugin_type, plugins in content.items():
        logging.info("Processing '%s' for README", plugin_type)
        if plugin_type == "modules":
            data.append("## Modules")
        else:
            data.append(
                "## {plugin_type} plugins".format(plugin_type=plugin_type.capitalize())
            )
        data.append("Name | Description")
        data.append("--- | ---")
        for plugin, description in sorted(plugins.items()):
            if plugin_type != "filter":
                link = "[{plugin}]({gh_url}/blob/master/docs/{plugin}.rst)".format(
                    gh_url=gh_url, plugin=plugin
                )
            else:
                link = plugin
            data.append(
                "{link}|{description}".format(
                    link=link, description=description.replace("|", "\\|")
                )
            )
    readme = os.path.join(path, "README.md")
    try:
        with open(readme) as f:
            content = f.read().splitlines()
    except FileNotFoundError:
        logging.error("README.md not found in %s", path)
        logging.error("README.md not updated")
        sys.exit(1)
    try:
        start = content.index("<!--start collection content-->")
        end = content.index("<!--end collection content-->")
    except ValueError as _err:
        logging.error("Content anchors not found in %s", readme)
        logging.error("README.md not updated")
        sys.exit(1)
    if start and end:
        new = content[0 : start + 1] + data + content[end:]
        with open(readme, "w") as fhand:
            fhand.write("\n".join(new))
        logging.info("README.md updated")


def handle_filters(collection, fullpath):
    """ Grab each filter from a filter plugin file and
    use the def comment if available

    :param collection: The full collection name
    :type collection: str
    :param fullpath: The full path to the filter plugin file
    :type fullpath: str
    :return: A doct of filter plugins + descriptions
    """

    plugins = {}
    with open(fullpath) as fhand:
        file_contents = fhand.read()
    module = ast.parse(file_contents)
    function_definitions = {
        node.name: ast.get_docstring(node)
        for node in module.body
        if isinstance(node, ast.FunctionDef)
    }
    classdef = [
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "FilterModule"
    ]
    if not classdef:
        return plugins

    filter_map = next(
        (
            node
            for node in classdef[0].body
            if isinstance(node, ast.Assign)
            and hasattr(node, "targets")
            and node.targets[0].id == "filter_map"
        ),
        None,
    )

    if not filter_map:
        filter_func = [
            func
            for func in classdef[0].body
            if isinstance(func, ast.FunctionDef) and func.name == "filters"
        ]
        if not filter_func:
            return plugins

        # The filter map is either looked up using the filter_map = {} assignment or if return returns a dict literal.
        filter_map = next(
            (
                node
                for node in filter_func[0].body
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
            ),
            None,
        )

    if not filter_map:
        return plugins

    keys = [k.value for k in filter_map.value.keys]
    logging.info("Adding filter plugins %s", ",".join(keys))
    values = [k.id for k in filter_map.value.values]
    filter_map = dict(zip(keys, values))
    for name, func in filter_map.items():
        if func in function_definitions:
            comment = function_definitions[
                func
            ] or "{collection} {name} filter plugin".format(
                collection=collection, name=name
            )

            # Get the first line from the docstring for the description and make that the short description.
            comment = next(
                c for c in comment.splitlines() if c and not c.startswith(":")
            )
            plugins[
                "{collection}.{name}".format(collection=collection, name=name)
            ] = comment
    return plugins


def process(collection, path):  # pylint: disable-msg=too-many-locals
    """
    Process the files in each subdirectory

    :param collection: The collection name
    :type collection: str
    :param path: The path to the collection
    :type path: str
    """
    template = jinja_environment()
    docs_path = Path(path, "docs")
    if docs_path.is_dir():
        logging.info("Purging content from directory %s", docs_path)
        shutil.rmtree(docs_path)
    logging.info("Making docs directory %s", docs_path)
    Path(docs_path).mkdir(parents=True, exist_ok=True)

    content = {}

    for subdir in SUBDIRS:
        if subdir == "modules":
            plugin_type = "module"
        else:
            plugin_type = subdir

        dirpath = Path(path, "plugins", subdir)
        if dirpath.is_dir():
            content[subdir] = {}
            logging.info("Process content in %s", dirpath)
            for filename in os.listdir(dirpath):
                if filename.endswith(".py") and filename not in IGNORE_FILES:
                    fullpath = Path(dirpath, filename)
                    logging.info("Processing %s", fullpath)
                    if subdir == "filter":
                        content[subdir].update(handle_filters(collection, fullpath))
                    else:
                        doc, examples, returndocs, metadata = plugin_docs.get_docstring(
                            fullpath, fragment_loader
                        )
                        if doc:
                            doc["plugin_type"] = plugin_type

                            if returndocs:
                                doc["returndocs"] = yaml.safe_load(returndocs)
                                convert_descriptions(doc["returndocs"])

                            doc["metadata"] = (metadata,)
                            if isinstance(examples, string_types):
                                doc["plainexamples"] = examples
                            else:
                                doc["examples"] = examples

                            doc["module"] = "{collection}.{plugin_name}".format(
                                collection=collection, plugin_name=doc[plugin_type]
                            )
                            doc["author"] = ensure_list(doc["author"])
                            doc["description"] = ensure_list(doc["description"])
                            try:
                                convert_descriptions(doc["options"])
                            except KeyError:
                                pass  # This module takes no options

                            module_rst_path = Path(path, "docs", doc["module"] + ".rst")

                            with open(module_rst_path, "w") as fd:
                                fd.write(template.render(doc))
                            content[subdir][doc["module"]] = doc["short_description"]
    return content


def load_galaxy(path):
    """ Load collection details from the galaxy.yml file in the collection

    :param path: The path the collection
    :return: The collection name and gh url
    """
    try:
        with open(Path(path, "galaxy.yml"), "r") as stream:
            try:
                return yaml.safe_load(stream)
            except yaml.YAMLError as _exc:
                logging.error("Unable to parse galaxy.yml in %s", path)
                sys.exit(1)
    except FileNotFoundError:
        logging.error("Unable to find galaxy.yml in %s", path)
        sys.exit(1)


def link_collection(path, galaxy):
    """ Link the provided collection into the Ansible default collection path

    :param path: A path
    :type path: str
    :param galaxy: The galaxy.yml contents
    :type galaxy: dict
    """

    collection_root = Path(Path.home(), ".ansible/collections/ansible_collections")
    namespace_directory = Path(collection_root, galaxy["namespace"])
    collection_directory = Path(namespace_directory, galaxy["name"])

    logging.info("Linking collection to user collection directory")
    logging.info(
        "This is required for the Ansible fragment loader to find doc fragments"
    )

    if collection_directory.exists():
        logging.info("Attempting to remove existing %s", collection_directory)

        if collection_directory.is_symlink():
            logging.info("Unlinking: %s", collection_directory)
            collection_directory.unlink()
        else:
            logging.info("Deleteing: %s", collection_directory)
            shutil.rmtree(collection_directory)

    logging.info("Creating namepsace directory %s", namespace_directory)
    namespace_directory.mkdir(parents=True, exist_ok=True)

    logging.info("Linking collection %s -> %s", path, collection_directory)
    collection_directory.symlink_to(path)


def main():
    """
    The entry point
    """
    parser = ArgumentParser()
    parser.add_argument(
        "-p",
        "--path",
        help="The path to the collection (ie ./ansible.netcommon",
        required=True,
    )
    args = parser.parse_args()
    path = Path(args.path).absolute()
    galaxy = load_galaxy(path=path)
    collection = "{namespace}.{name}".format(
        namespace=galaxy["namespace"], name=galaxy["name"]
    )
    logging.info("Setting collection name to %s", collection)
    gh_url = galaxy["repository"]
    logging.info("Setting github repository url to %s", gh_url)
    link_collection(path, galaxy)
    content = process(collection=collection, path=path)
    update_readme(content=content, path=args.path, gh_url=gh_url)


if __name__ == "__main__":
    main()
