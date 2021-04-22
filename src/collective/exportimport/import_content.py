# -*- coding: utf-8 -*-
from datetime import datetime
from DateTime import DateTime
from plone import api
from plone.api.exc import InvalidParameterError
from plone.i18n.normalizer.interfaces import IIDNormalizer
from plone.protect.interfaces import IDisableCSRFProtection
from plone.restapi.interfaces import IDeserializeFromJson
from Products.Five import BrowserView
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile
from zope.component import getMultiAdapter
from zope.component import getUtility
from zope.interface import alsoProvides
from ZPublisher.HTTPRequest import FileUpload

import json
import logging
import random
import six
import transaction

logger = logging.getLogger(__name__)


class ImportContent(BrowserView):

    template = ViewPageTemplateFile("templates/import_content.pt")

    # You can specify a default-target container for all items of a type.
    # Example {'News Item': '/imported-newsitems'}
    CONTAINER = {}

    # TODO
    BUGS = {}

    # These fields will be ignored
    # Exmaple: ['relatedItems']
    DROP_FIELDS = []

    # Items with these uid will be ignored
    # Example: ['04d1477583c74552a7fcd81a9085c620']
    DROP_UIDS = []

    # These paths will be ignored
    # Example: ['/Plone/doormat/', '/Plone/import_files/']
    DROP_PATHS = []

    # Default values for some fields
    # Example: {'which_price': 'normal'}
    DEFAULTS = {}

    def __call__(self, jsonfile=None, portal_type=None, return_json=False, limit=None):
        self.limit = limit
        response = self.request.response

        if not self.request.form.get("form.submitted", False):
            return self.template()

        if self.request.form.get("import_relations", False):
            return response.redirect("@@import_relations")

        if self.request.form.get("import_translations", False):
            return response.redirect("@@import_translations")

        if self.request.form.get("import_members", False):
            return response.redirect("@@import_members")

        if self.request.form.get("import_localroles", False):
            return response.redirect("@@import_localroles")

        if self.request.form.get("reset_modified_date", False):
            return response.redirect("@@reset_modified_date")

        if jsonfile:
            self.portal = api.portal.get()
            status = "success"
            try:
                if isinstance(jsonfile, str):
                    if not portal_type:
                        raise RuntimeError(
                            "portal_types required when passing a string"
                        )
                    self.portal_type = portal_type
                    return_json = True
                    data = json.loads(jsonfile)
                elif isinstance(jsonfile, FileUpload):
                    self.portal_type = jsonfile.filename.split(".json")[0]
                    data = json.loads(jsonfile.read())
                else:
                    raise ("Data is neither text nor upload.")
            except Exception as e:
                logger.error(e)
                status = "error"
                msg = e
                api.portal.show_message(
                    u"Exception during uplad: {}".format(e),
                    request=self.request,
                )
            else:
                msg = self.do_import(data)
                api.portal.show_message(msg, self.request)

        if return_json:
            msg = {"state": status, "msg": msg}
            return json.dumps(msg)
        return self.template()

    def do_import(self, data):
        start = datetime.now()
        added = self.import_new_content(data)
        transaction.commit()
        end = datetime.now()
        delta = end - start
        msg = u"Imported {} {} in {} seconds".format(
            len(added),
            self.portal_type,
            delta.seconds,
        )
        logger.info(msg)
        return msg

    def import_new_content(self, data):
        self.safe_portal_type = fix_portal_type(self.portal_type)
        added = []
        container = None
        container_path = self.CONTAINER.get(self.portal_type, None)
        if container_path:
            container = api.content.get(path=container_path)
            if not container:
                raise RuntimeError(
                    u"Target folder {} for type {} is missing".format(
                        container_path, self.portal_type
                    )
                )
        logger.info(u"Importing {} {}".format(len(data), self.portal_type))
        for index, item in enumerate(data, start=1):
            if self.limit and len(added) >= self.limit:
                break

            uuid = item["UID"]
            if uuid in self.DROP_UIDS:
                continue

            skip = False
            for drop in self.DROP_PATHS:
                if drop in item["@id"]:
                    skip = True
            if skip:
                continue

            if not index % 100:
                logger.info("Imported {} items...".format(index))

            new_id = item["@id"].split("/")[-1]
            if new_id != item["id"]:
                logger.info(
                    u"Conflicting ids in url ({}) and id ({}). Using {}".format(
                        new_id, item["id"], new_id
                    )
                )
                item["id"] = new_id

            item = self.handle_broken(item)
            if not item:
                continue
            item = self.handle_dropped(item)
            if not item:
                continue
            item = self.global_dict_hook(item)
            if not item:
                continue
            item = self.custom_dict_hook(item)
            if not item:
                continue

            container = self.handle_container(item) or container
            if not container:
                logger.info(u"No container found for {}".format(item["@id"]))
                continue

            # Speed up import by not using autogenerated ids for conflicts
            if new_id in container:
                duplicate = new_id
                new_id = "{}-{}".format(random.randint(1000, 9999), new_id)
                item["id"] = new_id
                logger.info(
                    u"{} ({}) already exists. Created as {}".format(
                        duplicate, item["@id"], new_id
                    )
                )

            factory_kwargs = item.get('factory_kwargs', {})
            container.invokeFactory(item["@type"], item["id"], **factory_kwargs)
            new = container[item["id"]]

            # import using plone.restapi deserializers
            deserializer = getMultiAdapter((new, self.request), IDeserializeFromJson)
            new = deserializer(validate_all=False, data=item)

            self.global_obj_hook(new, item)
            self.custom_obj_hook(new, item)

            uuid = self.set_uuid(item, new)
            if uuid != item["UID"]:
                item["UID"] = uuid

            if item["review_state"] and item["review_state"] != "private":
                try:
                    api.content.transition(to_state=item["review_state"], obj=new)
                except InvalidParameterError as e:
                    logger.info(e)

            # set modified-date as a custom attribute as last step
            modified = item.get("modified", item.get("modification_date", None))
            if modified:
                modified_data = datetime.strptime(modified, "%Y-%m-%dT%H:%M:%S%z")
                modification_date = DateTime(modified_data)
                new.modification_date = modification_date
                new.modification_date_migrated = modification_date
                # new.reindexObject(idxs=['modified'])
            logger.info("Created {} {}".format(new.absolute_url(), item["@type"]))
            added.append(new.absolute_url())
        return added

    def handle_broken(self, item):
        """Fix some invalid values."""
        if item["id"] not in self.BUGS:
            return item
        for key, value in self.BUGS[item["id"]].items():
            logger.info(
                "Replaced {} with {} for field {} of {}".format(
                    item[key], value, key, item["id"]
                )
            )
            item[key] = value
        return item

    def handle_dropped(self, item):
        """Drop some fields, especially relations."""
        for key in self.DROP_FIELDS:
            item.pop(key, None)
        return item

    def handle_defaults(self, item):
        """Set missing values especially for required fields."""
        for key in self.DEFAULTS:
            if not item.get(key, None):
                item[key] = self.DEFAULTS[key]
        return item

    def global_dict_hook(self, item):
        """Overwrite this do general changes on the dict before deserializing.

        Example:
        if not item['language'] and 'Plone/de/' in item['parent']['@id']:
            item['language'] = {'token': 'de', 'title': 'Deutsch'}
        elif not item['language'] and 'Plone/en/' in item['parent']['@id']:
            item['language'] = {'token': 'en', 'title': 'English'}
        elif not item['language'] and 'Plone/fr/' in item['parent']['@id']:
            item['language'] = {'token': 'fr', 'title': 'Français'}

        # drop layout property (we always use the type default view)
        item.pop('layout', None)
        """
        return item

    def custom_dict_hook(self, item):
        """Hook to inject dict-modifiers by type before deserializing.
        E.g.: dict_hook_document(self, item)
        """
        modifier = getattr(self, "dict_hook_{}".format(self.safe_portal_type), None)
        if modifier and callable(modifier):
            item = modifier(item)
        return item

    def global_obj_hook(self, obj, item):
        """Override hook to modify all items of the imported item by type."""
        return obj

    def custom_obj_hook(self, obj, item):
        """Hook to inject modifiers of the imported item by type.
        E.g.: obj_hook_newsitem(self, obj, item)
        """
        modifier = getattr(self, "obj_hook_{}".format(self.safe_portal_type), None)
        if modifier and callable(modifier):
            modifier(obj, item)

    def handle_container(self, item):
        """Specify a container per item and type using custom methods
        Example for content_type 'Document:

        def handle_document_container(self, item):
            lang = item['language']['token'] if item['language'] else ''
            base_path = self.CONTAINER[self.portal_type][item['language']['token']]
            folder = api.content.get(path=base_path)
            if not folder:
                raise RuntimeError(
                    f'Target folder {base_path} for type {self.portal_type} is missing'
                )
            parent_url = item['parent']['@id']
            parent_path = '/'.join(parent_url.split('/')[5:])
            if not parent_path:
                # handle elements in the language root
                return folder

            # create original structure for imported content
            for element in parent_path.split('/'):
                if element not in folder:
                    folder = api.content.create(
                        container=folder,
                        type='Folder',
                        id=element,
                        title=element,
                        language=lang,
                    )
                    logger.debug(
                        f'Created container {folder.absolute_url()} to hold {item["@id"]}'
                    )
                else:
                    folder = folder[element]

            return folder

        Example for Images:

        def handle_image_container(self, item):
            if '/produkt-bilder/' in item['@id']:
                return self.portal['produkt-bilder']

            if '/de/extranet/' in item['@id']:
                return self.portal['extranet']['de']['images']
            if '/en/extranet/' in item['@id']:
                return self.portal['extranet']['en']['images']
            if '/fr/extranet/' in item['@id']:
                return self.portal['extranet']['fr']['images']
            if '/de/' in item['@id']:
                return self.portal['de']['images']
            if '/en/' in item['@id']:
                return self.portal['en']['images']
            if '/fr/' in item['@id']:
                return self.portal['fr']['images']

            return self.portal['images']
        """
        if self.request.get("import_to_current_folder", None):
            return self.context
        method = getattr(
            self, "handle_{}_container".format(self.safe_portal_type), None
        )
        if method and callable(method):
            return method(item)
        else:
            # Default is to use the original containers is they exist
            return self.get_parent_as_container(item)

    def get_parent_as_container(self, item):
        """The default is to generate a folder-structure exactly as the original"""
        parent_url = item["parent"]["@id"]
        parent_path = "/".join(parent_url.split("/")[4:])
        parent_path = "/" + parent_path
        parent = api.content.get(path=parent_path)
        if parent:
            return parent
        else:
            return self.create_container(item)

    def create_container(self, item):
        folder = self.context
        parent_url = item["parent"]["@id"]
        parent_path = "/".join(parent_url.split("/")[5:])

        # create original structure for imported content
        for element in parent_path.split("/"):
            if element not in folder:
                folder = api.content.create(
                    container=folder,
                    type="Folder",
                    id=element,
                    title=element,
                )
                logger.debug(
                    u"Created container {} to hold {}".format(
                        folder.absolute_url(), item["@id"]
                    )
                )
            else:
                folder = folder[element]

        return folder

    def set_uuid(self, item, obj):
        uuid = item["UID"]
        if api.content.find(UID=uuid):
            # this should only happen if you run import multiple times
            uuid = obj.UID()
            logger.info(
                "UID {} of {} already in use by {}. Using {}".format(
                    item["UID"],
                    item["@id"],
                    api.content.get(UID=item["UID"]).absolute_url(),
                    uuid,
                ),
            )
        else:
            setattr(obj, "_plone.uuid", uuid)
            obj.reindexObject(idxs=["UID"])
        return uuid


def fix_portal_type(portal_type):
    normalizer = getUtility(IIDNormalizer)
    return normalizer.normalize(portal_type).replace("-", "")


class ResetModifiedDate(BrowserView):
    def __call__(self):
        portal = api.portal.get()
        alsoProvides(self.request, IDisableCSRFProtection)
        amount = 0

        def fix_modified(obj, path):
            if six.PY3:
                nonlocal amount
            modified = getattr(obj, "modification_date_migrated", None)
            if not modified:
                return
            if modified != obj.modification_date:
                obj.modification_date = modified
                # del obj.modification_date_migrated
                obj.reindexObject(idxs=["modified"])
                amount += 1

        portal.ZopeFindAndApply(portal, search_sub=True, apply_func=fix_modified)
        msg = "Reset modification date for {} items.".format(amount)
        api.portal.show_message(msg, self.request)
        return self.request.response.redirect(self.context.absolute_url())
