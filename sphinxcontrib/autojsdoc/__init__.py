"""
    sphinxcontrib.autojsdoc
    ~~~~~~~~~~~~~~~~~~~~~~~

    Automatically insert docstrings for JS functions, classes or whole modules into the doctree.

    - use JSDOC to build a structure.json file of your whole project.

         jsdoc -X -r src_dir > doc_src/jsdoc/structure.json

    - add to your conf.py

       .. code::

          extensions = [
             ...
             'sphinxcontrib.autojsdoc',
          ]

          ...

          autojsdoc_structure_json = 'doc_src/jsdoc/structure.json'
          autojsdoc_members = True
          autojsdoc_title = True

    - in your documentation use:

       .. code::

          .. js:automodule:: my/module_1 my/module_2

       where the arguments are regular expressions matched against the longname
       attribute in structure.json.

    :copyright: Copyright 2019 by Marcello Perathoner <marcello@perathoner.de>
    :license: BSD, see LICENSE for details.

"""

import collections
import json as jsonlib
import operator
import os
import re
from typing import Any, Callable, Dict, Iterator, List, Sequence, Set, Tuple, Union # noqa

import docutils
from docutils.parsers.rst import directives
from docutils.statemachine import StringList

import sphinx
from sphinx.util.docutils import SphinxDirective, switch_source_input
from sphinx.util.nodes import nested_parse_with_titles
from sphinx.errors import SphinxWarning, SphinxError, ExtensionError

import pbr.version

if False:
    # For type annotations
    from sphinx.application import Sphinx  # noqa

__version__ = pbr.version.VersionInfo ('autojsdoc').version_string ()

NAME = 'autojsdoc' # name of this sphinx plugin

logger = sphinx.util.logging.getLogger (__name__)

RE_AUTOSTRIP = re.compile (r'^js:auto') # strip directive name to obtain objtype
RE_ID_SEP    = re.compile (r'([.#~])')  # JSDoc uses these separators in identifiers
RE_BRACES    = re.compile (r'(\s*\(.*\))')
RE_WS        = re.compile (r'(\s+)')

loaded_structure_files = {}

def normalize_space (text):
    """ Replace all runs of whitespace with one space. """
    return RE_WS.sub (' ', text.strip ())

def strip_braces (text):
    """ Strip the braces from function signatures. """
    return RE_BRACES.sub ('', text)

def strip_directive (name):
    """ Strip directive name to obtain the obj type.

    'js:automodule' -> 'module',
    'js:autoclass'  -> 'class', ...
    """
    return RE_AUTOSTRIP.sub ('', name)

def bs (link):
    """ Replace \\ with \\\\ because RST wants it that way. """
    return link.replace ('\\', '\\\\')


def setup (app):
    # type: (Sphinx) -> Dict[unicode, Any]

    # app.add_directive_to_domain ('js', 'autopackage',  AutoDirective)
    app.add_directive_to_domain ('js', 'automodule',   AutoDirective)
    app.add_directive_to_domain ('js', 'autoclass',    AutoDirective, override = True)
    app.add_directive_to_domain ('js', 'autofunction', AutoDirective, override = True)

    app.add_config_value (NAME + '_structure_json', '', False)
    app.add_config_value (NAME + '_members', False, False)
    app.add_config_value (NAME + '_title', False, False)

    return {
        'version'            : __version__,
        'parallel_read_safe' : True,
    }


def members_option (arg: Any) -> Union[bool, List[str]]:
    """Used to convert the :members: option to auto directives."""
    if arg is None or arg is True:
        return True
    if arg is False:
        return False
    return [x.strip () for x in arg.split (',')]

def bool_option (arg: Any) -> bool:
    """Used to convert flag options to auto directives.  (Instead of
    directives.flag(), which returns None).
    """
    return True


class AutoJSDocError (SphinxError):
    """ The autojsdoc exception. """
    category = NAME + ' error'


class obj (object):
    """ Represents any object read in from the structure.json file. """

    def __init__ (self, d):
        """ Initialize with members of d.

        :param d: Either an object or a dictionary.
        """
        try:
            self.__dict__.update (d.__dict__)
        except:
            self.__dict__.update (d)

    def __contains__ (self, key):
        return key in self.__dict__


class Obj (obj):
    """ Represents an object with a kind read in from the structure.json file. """

    def __init__ (self, d):
        self.ignore       = False
        self.undocumented = False
        self.longname     = None
        self.name         = None
        self.memberof     = None
        self.description  = ''

        super ().__init__ (d)
        self.parent    = None
        self.children  = []

    def __contains__ (self, key):
        return key in self.__dict__

    def doc (self):
        return not self.undocumented

    def get_source_line (self):
        """
        Return the "source" and "line" attributes from the `node` given or from
        its closest ancestor.
        """

        # pylint: disable=no-member
        if 'meta' in self:
            return os.path.join (self.meta.path, self.meta.filename), self.meta.lineno
        if self.parent:
            return self.parent.get_source_line ()
        return '<unknown>', 0

    def error (self, msg):
        logger.error (msg, location = '%s:%d' % self.get_source_line ())

    def warn (self, msg):
        logger.warn (msg, location = '%s:%d' % self.get_source_line ())

    def splitlines (self, text, indent):
        return [(' ' * indent) + s for s in text.splitlines ()]

    def nl (self, directive):
        directive.content.append ('', '')

    def append (self, text, directive, indent):
        filename, sourceline = self.get_source_line ()
        if isinstance (text, str):
            text = self.splitlines (text, indent)
        for line in text:
            directive.content.append (
                line,
                '%s:%d:<%s>' % (filename, sourceline, NAME)
            )

    def append_desc (self, directive, indent):
        self.append (self.get_description (), directive, indent)
        self.nl (directive)

    def underscore (self, text, char, directive, indent):
        self.append (text, directive, indent)
        self.append (char * len (text), directive, indent)
        self.nl (directive)

    def get_name (self):
        return self.name

    def get_value (self):
        try:
            return self.meta.code.value
        except:
            return ''

    def get_longname (self):
        return self.longname

    def get_description (self):
        return normalize_space (self.description)

    def run (self, directive, indent):
        for node in self.children:
            node.run (directive, indent)
        self.nl (directive)


class JSSee (Obj):

    def __init__ (self, d):
        self.link = ''
        super ().__init__ (d)

    def run (self, directive, indent):
        link = self.link
        if '://' not in link:
            link = directive.xref (link)

        self.append ("See: %s %s" % (link, self.get_description ()), directive, indent)
        self.nl (directive)


class JSWithTypes (Obj):
    """ A javascript object that has types. """

    def __init__ (self, d):
        self.type = obj ({ 'names' : [] })
        super ().__init__ (d)

    def get_types (self):
        return bs ('|'.join (self.type.names))


class JSArgument (JSWithTypes):

    def run (self, directive, indent):
        name = self.name        if 'name'        in self else ''
        self.append (":param %s %s: %s" % (
            self.get_types (), name, self.get_description ()), directive, indent)


class JSReturns (JSWithTypes):

    def run (self, directive, indent):
        desc = self.get_description ()
        if desc:
            self.append (":returns: %s" % desc, directive, indent)
        types = self.get_types ()
        if types:
            self.append (":rtype: %s" % directive.xref (types), directive, indent)


class JSThrows (JSWithTypes):

    def run (self, directive, indent):
        self.append (":raises %s: %s" % (directive.xref (
            self.get_types ()), self.get_description ()), directive, indent)


class JSVariable (JSWithTypes):

    def run (self, directive, indent):
        indent += 3
        types = self.get_types ()
        if types:
            self.append ("(%s)" % directive.xref (types), directive, indent)
        self.append_desc (directive, indent)


class JSConstant (JSVariable):

    def run (self, directive, indent):
        if self.doc ():
            self.append (".. js:data:: %s" % self.get_name (), directive, indent)
            self.nl (directive)

            value = self.get_value ()
            if value:
                self.append ('.. code:: javascript', directive, indent + 3)
                self.nl (directive)
                self.append (value, directive, indent + 6)
                self.nl (directive)

            super ().run (directive, indent)


class JSAttribute (JSVariable):

    def run (self, directive, indent):
        if self.doc ():
            self.append (".. js:attribute:: %s" % self.get_name (), directive, indent)
            self.nl (directive)
            super ().run (directive, indent)


class JSCallable (Obj):

    def __init__ (self, d):
        self.params  = []
        self.returns = []
        super ().__init__ (d)

    def get_signature (self):
        if 'params' in self:
            args = [ p.name for p in self.params ]
            return "%s (%s)" % (self.get_name (), ', '.join (args))
        return self.get_name ()

    def run (self, directive, indent):
        indent += 3

        self.append_desc (directive, indent)

        if 'params' in self:
            for param in self.params:
                JSArgument (param).run (directive, indent)
            self.nl (directive)

        if 'returns' in self:
            for ret in self.returns:
                JSReturns (ret).run (directive, indent)
            self.nl (directive)


class JSFunction (JSCallable):
    def run (self, directive, indent):
        if self.doc ():
            self.append (".. js:function:: %s" % self.get_signature (), directive, indent)
            self.nl (directive)
            super ().run (directive, indent)


class JSMethod (JSCallable):
    def run (self, directive, indent):
        if self.doc ():
            self.append (".. js:method:: %s" % self.get_signature (), directive, indent)
            self.nl (directive)
            super ().run (directive, indent)


class JSClass (Obj):
    def run (self, directive, indent):
        if self.doc ():
            self.append (".. js:class:: %s" % self.get_name (), directive, indent)
            self.nl (directive)
            indent += 3

            self.append_desc (directive, indent)

            super ().run (directive, indent)


class JSFile (Obj):
    def run (self, directive, indent):

        self.append ("File: %s" % self.get_name (), directive, indent)
        self.nl (directive)

        self.append_desc (directive, indent)

        super ().run (directive, indent)


class JSPackage (Obj):

    def __init__ (self, d):
        self.package = ''
        self.files   = []
        super ().__init__ (d)

    def run (self, directive, indent):

        for file in self.files:
            file.run (directive, indent)

        super ().run (directive, indent)


class JSModule (Obj):
    """This only works if there are @module tags in the files.

    .. warning::

       Never put @file and @module into the same comment block.  Use two
       blocks.

    """

    def run (self, directive, indent):
        module = self.get_longname ()
        if module.startswith ('module:'):
            module = module[7:]

        self.append (".. js:module:: %s" % module, directive, indent)
        self.nl (directive)

        if directive.get_opt ('title'):
            # add a section title
            self.underscore (module, '-', directive, indent)
            self.nl (directive)

        self.append_desc (directive, indent)

        members = directive.get_opt ('members')
        if members is True:
            super ().run (directive, indent)
        if isinstance (members, list):
            for c in self.children:
                if c.get_name () in members:
                    c.run (directive, indent)


class AutoDirective (SphinxDirective):
    """Directive to document a JS 'object'. """

    required_arguments = 1
    """Modules to autodoc.  A regex that matches @module tags in jsdoc."""

    optional_arguments = 999
    """More regexes that match @module tags.  If a following regex matches the same
    module as a previous regex, the module will not be output again.  You may
    match the main module first and then match all modules.  That will output
    all modules in alphabetical order, but the main module at the top.

    """

    has_content = False

    option_spec = {
        'structure_json' : directives.unchanged,
        'members'        : members_option,
        'title'          : bool_option,
    }
    """structure_json
          Path of the structure.json file.
          Defaults to the config option autojsdoc_structure_json.
          Required.

       members
          Should the members of the documented structure be autodoced too?
          Defaults to the config option autojsdoc_members.
          Default: False.

       title
          Should the directive output a section title?
          Defaults to the config option autojsdoc_title.
          Default: False.
    """

    vtable = {
        'class'    : JSClass,
        'constant' : JSConstant,
        'file'     : JSFile,
        'function' : JSFunction,
        'member'   : JSAttribute, # eg. variable in function
        'method'   : JSMethod,
        'module'   : JSModule,
        'package'  : JSPackage,
    }
    """ Table JSDoc @kind => class """

    xref_table = {
        'class'    : 'js:class',
        'constant' : 'js:data',
        'function' : 'js:func',
        'member'   : 'js:attr',
        'method'   : 'js:meth',
        'module'   : 'js:mod',
    }
    """ Table JSDoc @kind => js:ref """


    def get_opt (self, name, required = False):
        opt = self.options.get (name) or getattr (self.env.config, "%s_%s" % (NAME, name))
        if required and not opt:
            raise AutoJSDocError (
                ':%s: option required in directive (or set %s_%s in conf.py).' % (name, NAME, name)
            )
        return opt


    def xref (self, name):
        """ Find the correct incantation to xref things.

        Eg. if name is the name of a function, then output

          :js:func:`name`

        Used mainly to xref parameter and return types.

        :param string name: The name of a custom object.
        """

        kind = None
        if name in self.longnames:
            kind = self.longnames[name].kind
        elif name in self.names:
            kind = self.names[name].kind
        if kind:
            return ":%s:`%s`" % (self.xref_table[kind], name)
        return name


    def check_params (self, doclet):
        """ Check for undocumented or incongruous params. """

        if not doclet.undocumented:
            try:
                param_names      = { p.name for p in doclet.params }
                meta_param_names = set (doclet.meta.code.paramnames)

                for name in param_names - meta_param_names:
                    doclet.warn ("Documented parameter %s not found on signature" % name)

                for name in meta_param_names - param_names:
                    doclet.warn ("Undocumented parameter %s" % name)

            except AttributeError:
                pass

        return doclet


    def make_forest (self, doclets):
        """The structure.json file contains a flat list of doclets.  Make it into a
        forest of trees by adding parent and children attributes to each doclet
        according to the attribute @memberof.

        :param list doclets: a flat list of doclets

        """

        for o in doclets:
            if o.memberof is None:
                continue
            if o.memberof in self.longnames:
                o.parent = self.longnames[o.memberof]
                o.parent.children.append (o)
                continue
            if o.doc ():
                if o.memberof == '<anonymous>':
                    o.error ("""Could not link up object %s to %s.
                             Try giving the anonymous object an @alias."""
                             % (o.longname, o.memberof))
                else:
                    o.error ("Could not link up object %s to %s" % (o.longname, o.memberof))


    def merge_doclets (self, doclets):
        """Occasionally JSDoc outputs one doclet for the object docblock and another
        doclet for the object code (eg. if the docblock contains a @function tag
        and is followed by a function.)  These represent the human view and the
        compiler view of things respectively.

        The same happens for exported objects. the 'export' keyword seems to get
        its own doclet.

        Here we merge all doclets with the same longname into one.

        """

        merged = []
        names = {}
        longnames = {}

        for doclet in doclets:
            last_doclet = self.longnames.setdefault (doclet.longname, doclet)

            if doclet is last_doclet:
                # first time seen
                self.names[doclet.name] = doclet
                merged.append (self.check_params (doclet))
            else:
                last_doclet.undocumented &= doclet.undocumented
                last_doclet.comment      += doclet.comment
                last_doclet.description  += doclet.description
                last_doclet.meta         =  doclet.meta  # prefer compilers view

        self.make_forest (merged)
        return merged, names, longnames


    def run (self):
        self.doclets = []
        self.names = {}
        self.longnames = {}

        structure_json = self.get_opt ('structure_json', True)

        parent = docutils.nodes.section ()
        parent.document = self.state.document
        objtype = strip_directive (self.name)  # 'js:automodule' => 'module'

        self.content = StringList ()

        def obj_factory (d):
            """ Transmogrify the dictionaries read from the json file into objects.
            If the object has a known kind make it into a JS<kind> class,
            else if it has an unknwon kind make it into an Obj
            else if it has no kind (substructures of doclets) make it an obj
            """
            try:
                kind = d['kind']
                o = self.vtable.get (kind, Obj) (d)
            except KeyError:
                o = obj (d)
            return o

        # load and cache structure file
        if structure_json not in loaded_structure_files:
            with open (structure_json, 'r') as fp:
                self.state.document.settings.record_dependencies.add (structure_json)
                loaded_structure_files[structure_json] = \
                    self.merge_doclets (jsonlib.load (fp, object_hook = obj_factory))

        # get cached structure file
        self.doclets, self.names, self.longnames = loaded_structure_files[structure_json]

        try:
            visited = set () # remember which objects we have already output
            for argument in self.arguments:
                rex = re.compile (argument)

                # grep the list of doclets
                doclets = (
                    d for d in self.doclets
                    if d.kind == objtype
                    and rex.search (d.longname)
                    and d.longname not in visited
                )

                for d in sorted (doclets, key = operator.attrgetter ('longname')):
                    visited.add (d.longname)
                    d.run (self, 0)

            with switch_source_input (self.state, self.content):
                # logger.info (self.content.pprint ())
                try:
                    nested_parse_with_titles (self.state, self.content, parent)
                except:
                    logger.error (self.content.pprint ())
                    raise

        except AutoJSDocError as exc:
            logger.error ('Error in "%s" directive: %s.' % (self.name, str (exc)))

        return parent.children
