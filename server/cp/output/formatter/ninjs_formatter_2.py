# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""NINJS formatter_2 for The Canadian Press's Superdesk

.. versionadded:: 1.8
    Added *source* field.

.. versionadded:: 1.7
    Added *ednote* field.
    Added *signal* field.
    Added *genre* field.

.. versionchanged:: 1.7
    Fixed copyrightholder/copyrightnotice handling to be consistent with newsml.
    Fixed place property qcode should be code.
    Output profile name instead of _id in profile field.


.. versionadded:: 1.6
    Added *evolvedfrom* field to ninjs output.

"""


import json
import superdesk
import logging
import re
from typing import Tuple

from flask import current_app as app
from eve.utils import config
from superdesk.publish.formatters import Formatter
from superdesk.errors import FormatterError
from superdesk.metadata.item import (
    ITEM_TYPE,
    CONTENT_TYPE,
    EMBARGO,
    GUID_FIELD,
    ASSOCIATIONS,
)
from superdesk.metadata.packages import RESIDREF, GROUP_ID, GROUPS, ROOT_GROUP, REFS
from superdesk.utils import json_serialize_datetime_objectId
from superdesk.media.renditions import get_renditions_spec
from superdesk.vocabularies import is_related_content
from apps.archive.common import get_utc_schedule
from superdesk import text_utils
from superdesk.attachments import get_attachment_public_url
import superdesk
from collections import OrderedDict

logger = logging.getLogger(__name__)
# this regex match the way custom media fields are put in associations (i.e. how the key
# is generated). This is legacy, and can hardly be changed without risking to break
# instances in production.
MEDIA_FIELD_RE = re.compile(r"(?P<field_id>\S+)--(?P<version>\d+)$")
EXTRA_ITEMS = "extra_items"

SCHEME_MAP = {
    "sig": "http://cv.iptc.org/newscodes/signal/",
}


def filter_empty_vals(data):
    """Filter out `None` values from a given dict."""
    return dict(filter(lambda x: x[1], data.items()))


def get_locale_name(item, language):
    try:
        return item["translations"]["name"][language]
    except (KeyError, TypeError):
        return item.get("name", "")


def format_cv_item(item, language):
    """Format item from controlled vocabulary for output."""
    if item.get("scheme") == "subject":
        return filter_empty_vals(
            {
                "code": item.get("qcode"),
                "name": get_locale_name(item, language),
                "scheme": "http://cv.iptc.org/newscodes/mediatopic/",
                "creator": item.get("creator", ""),
                "relevance": item.get("relevance", 100),
            }
        )
    else:
        return filter_empty_vals(
            {
                "code": item.get("qcode"),
                "name": get_locale_name(item, language),
                "scheme": item.get("scheme"),
                "creator": item.get("creator", ""),
                "relevance": item.get("relevance", 100),
            }
        )


class NINJSFormatter_2(Formatter):
    """
    The schema we use for the ninjs format is an extension
    of `the standard ninjs schema <http://www.iptc.org/std/ninjs/ninjs-schema_1.1.json>`_.

    *Changes from ninjs schema*:

    * ``uri`` was replaced by ``guid``: ``uri`` should be the resource identifier on the web
        but since the item was not published yet it can't be determined at this point
    * added ``priority`` field
    * added ``service`` field
    * added ``slugline`` field
    * added ``keywords`` field
    * added ``evolvedfrom`` field
    * added ``source`` field

    Associations dictionary may contain entire items like
    in `ninjs example <http://dev.iptc.org/ninjs-Examples-3>`_ or just the item ``guid``
    and ``type``. In the latest case the items are sent separately before the package item.
    """

    name = "NINJSv3"
    type = "ninjs3"

    direct_copy_properties: Tuple[str, ...] = (
        "versioncreated",
        "usageterms",
        "language",
        "headline",
        "copyrightnotice",
        "urgency",
        "pubstatus",
        "mimetype",
        "copyrightholder",
        "ednote",
        "body_text",
        "body_html",
        "slugline",
        "keywords",
        "firstcreated",
        "firstpublished",
        "source",
        "extra",
        "annotations",
    )

    rendition_properties = ("href", "width", "height", "mimetype", "poi", "media")
    vidible_fields = {field: field for field in rendition_properties}
    vidible_fields.update(
        {
            "url": "href",
            "duration": "duration",
            "mimeType": "mimetype",
            "size": "size",
        }
    )

    def __init__(self):
        self.can_preview = True
        self.can_export = True
        self.internal_renditions = ["original"]

    def format(self, article, subscriber, codes=None):
        try:
            pub_seq_num = superdesk.get_resource_service(
                "subscribers"
            ).generate_sequence_number(subscriber)

            ninjs = self._transform_to_ninjs(article, subscriber)
            return [
                (
                    pub_seq_num,
                    json.dumps(ninjs, default=json_serialize_datetime_objectId),
                )
            ]
        except Exception as ex:
            raise FormatterError.ninjsFormatterError(ex, subscriber)

    # Adding a method to fetch Parents of Manual Tags

    def _add_parent_manual_tags(self, item):
        cv = superdesk.get_resource_service("vocabularies").find_one(
            req=None, _id="subject_custom"
        )
        vocab_items = cv.get("items", [])
        vocab_mapping = {v["qcode"]: v for v in vocab_items}

        def find_youngest_parent(qcode):
            parent_qcode = vocab_mapping[qcode]["parent"]
            while parent_qcode:
                if vocab_mapping[parent_qcode]["in_jimi"]:
                    return vocab_mapping[
                        parent_qcode
                    ]  # Return the first parent where in_jimi is true
                parent_qcode = vocab_mapping.get(parent_qcode, {}).get("parent", None)
            return None

        updated_subjects = item.get(
            "subject", []
        ).copy()  # Copy the current subjects to avoid direct modification

        for subject in item.get("subject", []):
            if "qcode" in subject and subject["qcode"] in vocab_mapping:
                youngest_parent = find_youngest_parent(subject["qcode"])
                if youngest_parent and youngest_parent["qcode"] not in [
                    s["qcode"] for s in updated_subjects
                ]:
                    # Add the first parent tag where in_jimi is true to the item's subjects
                    updated_subjects.append(youngest_parent)

        item["subject"] = updated_subjects
        return item

    def _transform_to_ninjs(self, article, subscriber, recursive=True):
        # Using the method we created to fetch Parents of all Manual Tags

        article = self._add_parent_manual_tags(article)

        ninjs = {
            "guid": article.get(GUID_FIELD, article.get("uri")),
            "version": str(article.get(config.VERSION, 1)),
            "type": self._get_type(article),
        }

        if article.get("byline"):
            ninjs["byline"] = article["byline"]

        located = article.get("dateline", {}).get("located", {})
        if located:
            ninjs["located"] = located.get("city", "")

        for copy_property in self.direct_copy_properties:
            if article.get(copy_property) is not None:
                ninjs[copy_property] = article[copy_property]

        if "body_text" not in article and "alt_text" in article:
            ninjs["body_text"] = article["alt_text"]

        if "title" in article:
            ninjs["headline"] = article["title"]

        if article.get("body_html"):
            ninjs["body_html"] = self.append_body_footer(article)

        if article.get("description"):
            ninjs["description_html"] = self.append_body_footer(article)

        if article.get("place"):
            ninjs["place"] = self._format_place(article)

        if article.get("profile"):
            ninjs["profile"] = self._format_profile(article["profile"])

        extra_items = None
        # Updated the output for associations HERE
        if article.get("associations"):
            ninjs["associations"] = self._get_associations(article, subscriber)

        if article.get("embargoed"):
            ninjs["embargoed"] = article["embargoed"].isoformat()

        if article.get(EMBARGO):  # embargo set in superdesk overrides ingested one
            ninjs["embargoed"] = get_utc_schedule(article, EMBARGO).isoformat()

        if article.get("priority"):
            ninjs["priority"] = article["priority"]
        else:
            ninjs["priority"] = 5

        # Merging Various Entities into Subjects for ninjs Response
        # ---------------------------------------------------------
        # This section of the code is responsible for aggregating different entity types
        # like 'organisation', 'place', 'event', and 'person' along with 'subject' into
        # a single list.

        if (
            article.get("subject")
            or article.get("organisation")
            or article.get("place")
            or article.get("event")
            or article.get("person")
        ):
            combined_subjects = (
                self._get_subject(article)
                + self._get_organisation(article)
                + self._get_place(article)
                + self._get_event(article)
                + self._get_person(article)
            )
            ninjs["subject"] = combined_subjects

        if article.get("anpa_category"):
            ninjs["service"] = self._get_service(article)
        if article.get("renditions"):
            ninjs["renditions"] = self._get_renditions(article)
        elif "url" in article:
            ninjs["renditions"] = self._generate_renditions(article)

        if "order" in article:
            ninjs["order"] = article["order"]

        # SDPA-317
        if article.get("abstract"):
            ninjs["description_html"] = article["abstract"]
            ninjs["description_text"] = text_utils.get_text(article["abstract"])
        elif article.get("description_text"):
            ninjs["description_text"] = article["description_text"]
            ninjs["description_html"] = article.get(
                "description_html"
            ) or "<p>{}</p>".format(article["description_text"])
        elif "abstract" in article:  # BC
            ninjs["description_text"] = ninjs["description_html"] = ""

        if article.get("company_codes"):
            ninjs["organisation"] = [
                {
                    "name": c.get("name", ""),
                    "rel": "Securities Identifier",
                    "symbols": [
                        {
                            "ticker": c.get("qcode", ""),
                            "exchange": c.get("security_exchange", ""),
                        }
                    ],
                }
                for c in article["company_codes"]
            ]
        elif "company" in article:
            ninjs["organisation"] = [{"name": article["company"]}]

        if article.get("rewrite_of"):
            ninjs["evolvedfrom"] = article["rewrite_of"]

        if (
            not ninjs.get("copyrightholder")
            and not ninjs.get("copyrightnotice")
            and not ninjs.get("usageterms")
        ):
            ninjs.update(
                superdesk.get_resource_service("vocabularies").get_rightsinfo(article)
            )

        if article.get("genre"):
            ninjs["genre"] = self._get_genre(article)

        if article.get("flags", {}).get("marked_for_legal"):
            ninjs["signal"] = self._format_signal_cwarn()

        if article.get("signal"):
            ninjs.setdefault("signal", []).extend(
                [self._format_signal(signal) for signal in article["signal"]]
            )

        if article.get("attachments"):
            ninjs["attachments"] = self._format_attachments(article)

        if ninjs["type"] == CONTENT_TYPE.TEXT and (
            "body_html" in ninjs or "body_text" in ninjs
        ):
            if "body_html" in ninjs:
                body_html = ninjs["body_html"]
                word_count = text_utils.get_word_count(body_html)
                char_count = text_utils.get_char_count(body_html)
                readtime = text_utils.get_reading_time(
                    body_html, word_count, article.get("language")
                )
            else:
                body_text = ninjs["body_text"]
                word_count = text_utils.get_text_word_count(body_text)
                char_count = len(body_text)
                readtime = text_utils.get_reading_time(
                    body_text, word_count, article.get("language")
                )
            ninjs["charcount"] = char_count
            ninjs["wordcount"] = word_count
            ninjs["readtime"] = readtime

        if article.get("authors"):
            ninjs["authors"] = self._format_authors(article)

        if (article.get("schedule_settings") or {}).get("utc_publish_schedule"):
            ninjs["publish_schedule"] = article["schedule_settings"][
                "utc_publish_schedule"
            ]

        #  Added Code to create Original_id attribute
        if article.get("family_id"):
            ninjs["original_id"] = article["family_id"]

        # set description for custom embed field
        if article.get("extra"):
            ninjs["extra"] = article["extra"]
            for key, value in ninjs["extra"].items():
                if isinstance(value, dict) and "embed" in value:
                    value.setdefault("description", "")

        # Method to Append Jimi Tags in Subjects
        self.update_ninjs_subjects(ninjs, language="en-CA")

        return ninjs

    def _generate_renditions(self, article):
        """
        For associated items that have custom structure generate renditions based on the item `custom properties.
        """
        renditions = {"original": {}}
        for orig_field, dest_field in self.vidible_fields.items():
            if orig_field in article:
                renditions["original"][dest_field] = article[orig_field]
        if "thumbnail" in article:
            renditions["thumbnail"] = {"href": article["thumbnail"]}
        return renditions

    def _get_type(self, article):
        if article[ITEM_TYPE] == CONTENT_TYPE.PREFORMATTED:
            return CONTENT_TYPE.TEXT
        return article[ITEM_TYPE]

    # Updated _get_association to work with both Pictures and Text
    def _get_associations(self, article, subscriber):
        associations = {}
        article_type = self._get_type(article)

        if article_type == "text":
            for key, value in article.get("associations", {}).items():
                if value and "_id" in value:
                    associations[key] = {"guid": value["_id"]}

            return associations

        elif article_type == "picture":
            for group in article.get(GROUPS, []):
                if group[GROUP_ID] == ROOT_GROUP:
                    continue

            group_items = []
            for ref in group[REFS]:
                if RESIDREF in ref:
                    item = {}
                    item["guid"] = ref[RESIDREF]
                    item[ITEM_TYPE] = ref.get(ITEM_TYPE, "text")
                    if "label" in ref:
                        item["label"] = ref.get("label")
                    if ref.get("package_item"):
                        item.update(
                            self._transform_to_ninjs(
                                ref["package_item"], subscriber, recursive=False
                            )
                        )
                    group_items.append(item)
            if len(group_items) == 1:
                associations[group[GROUP_ID]] = group_items[0]
            elif len(group_items) > 1:
                for index in range(0, len(group_items)):
                    associations[group[GROUP_ID] + "-" + str(index)] = group_items[
                        index
                    ]

            return associations

    def _format_related(self, article, subscriber):
        """Format all associated items for simple items (not packages)."""
        associations = OrderedDict()
        extra_items = {}
        media = {}
        content_profile = None
        archive_service = superdesk.get_resource_service("archive")

        article_associations = OrderedDict(
            sorted(
                article.get(ASSOCIATIONS, {}).items(),
                key=lambda itm: (itm[1] or {}).get("order", 1),
            )
        )

        for key, item in article_associations.items():
            if item:
                if is_related_content(key) and "_type" not in item:
                    orig_item = archive_service.find_one(req=None, _id=item["_id"])
                    orig_item["order"] = item.get("order", 1)
                    item = orig_item.copy()

                item = self._transform_to_ninjs(item, subscriber, recursive=False)

                # Keep original POI and get rid of all other POI.
                renditions = item.get("renditions")
                if renditions:
                    for rendition in renditions.keys():
                        if rendition != "original" and renditions.get(
                            rendition, {}
                        ).get("poi"):
                            renditions[rendition].pop("poi", None)

                associations[key] = item  # all items should stay in associations
                match = MEDIA_FIELD_RE.match(key)
                if match:
                    # item id seems to be build from a custom id
                    # we now check content profile to see if it correspond to a custom field
                    if content_profile is None:
                        try:
                            profile = article["profile"]
                        except KeyError:
                            logger.warning(
                                "missing profile in article (guid: {guid})".format(
                                    guid=article.get("guid")
                                )
                            )
                            content_profile = {"schema": {}}
                        else:
                            content_profile = superdesk.get_resource_service(
                                "content_types"
                            ).find_one(_id=profile, req=None)
                    field_id = match.group("field_id")
                    schema = content_profile["schema"].get(field_id, {})
                    if (
                        schema.get("type") == "media"
                        or schema.get("type") == "related_content"
                    ):
                        # we want custom media fields in "extra_items", cf. SDESK-2955
                        version = match.group("version")
                        media.setdefault(field_id, []).append((version, item))
                        extra_items[field_id] = {"type": schema.get("type")}

        if media:
            # we have custom media fields, we now order them
            # and add them to "extra_items"
            for field_id, data in media.items():
                default_order = 1
                items_to_sort = [d[1] for d in sorted(data)]

                if extra_items[field_id]["type"] == "media":
                    # for media items default order is 0 and for related-content default order is 1
                    default_order = 0

                extra_items[field_id]["items"] = sorted(
                    items_to_sort, key=lambda item: item.get("order", default_order)
                )
        return associations, extra_items

    def _get_genre(self, article):
        lang = article.get("language", "")
        return [format_cv_item(item, lang) for item in article["genre"]]

    def update_ninjs_subjects(self, ninjs, language="en-CA"):
        capital_subjects = [
            "HIV and AIDS",
            "traditional Chinese medicine",
            "Buddhism",
            "Christianity",
            "Mormonism",
            "Christian Orthodoxy",
            "Protestantism",
            "Confucianism",
            "Hinduism",
            "Islam",
            "Jainism",
            "Judaism",
            "Zoroastrianism",
            "Scientology",
            "Shintoism",
            "Sikhism",
            "Taoism",
            "Unificationism",
            "Christmas",
            "Easter",
            "Pentecost",
            "Ramadan",
            "Yom Kippur",
            "Bible",
            "Qur'an",
            "Torah",
            "Dating and Relationships",
            "LGBTQ",
            "American football",
            "Australian rules football",
            "Canadian football",
            "Gaelic football",
            "Jai Alai (Pelota)",
            "Taekwon-Do",
            "Swiss wrestling",
            "Nordic combined",
            "Telemark skiing",
            "Olympic Games",
            "Paralympic Games",
            "eSports",
            "environmental, social and governance policy (ESG)",
            "Midsummer",
            "National day",
            "New year",
            "Halloween",
            "All Saints Day",
            "Walpurgis night",
            "stand up paddleboarding (SUP)",
            "Catholicism",
            "Shia Islam",
            "Sunni Islam",
            "Eid al-Adha",
            "Hasidism",
            "Hanukkah",
        ]
        try:
            # Fetch the vocabulary
            cv = superdesk.get_resource_service("vocabularies").find_one(
                req=None, _id="subject_custom"
            )
            vocab_items = cv.get("items", [])
            vocab_mapping = {}

            for item in vocab_items:
                if item.get("in_jimi") is True:
                    name_in_vocab = item.get("name")
                    qcode = item.get("qcode")
                    creator = item.get("creator", "")
                    relevance = item.get("relevance", 47)
                    translated_name = (
                        item.get("translations", {})
                        .get("name", {})
                        .get(language, name_in_vocab)
                    )
                    vocab_mapping[name_in_vocab.lower()] = (qcode, translated_name)

            updated_subjects = list(ninjs["subject"])

            for subject in ninjs["subject"]:
                subject_name = subject.get("name").lower()
                if subject_name in vocab_mapping:
                    qcode, translated_name = vocab_mapping[subject_name]
                    updated_subjects.append(
                        {
                            "code": qcode,
                            "name": translated_name,
                            "scheme": "http://cv.cp.org/cp-subject-legacy/",
                            "creator": creator,
                            "relevance": relevance,
                        }
                    )

            ninjs["subject"] = [
                {
                    **subject,
                    "name": (
                        subject["name"].lower()
                        if subject.get("scheme")
                        == "http://cv.iptc.org/newscodes/mediatopic/"
                        and subject["name"] not in capital_subjects
                        else subject["name"]
                    ),
                    "scheme": (
                        "subject_custom"
                        if subject.get("scheme")
                        == "http://cv.iptc.org/newscodes/mediatopic/"
                        or subject.get("scheme") is None
                        else subject.get("scheme")
                    ),
                }
                for subject in updated_subjects
            ]

        except Exception as e:
            logger.error(f"An error occurred. We are in ninjs exception: {str(e)}")

    def _get_subject(self, article):
        """Get subject list for article."""
        return [
            format_cv_item(item, article.get("language", ""))
            for item in article.get("subject", [])
        ]

    #  Updated Code here to fetch Organisations from Article
    def _get_organisation(self, article):
        return [
            format_cv_item(item, article.get("language", ""))
            for item in article.get("organisation", [])
        ]

    #  Updated Code here to fetch Places from Article
    def _get_place(self, article):
        """Get place list for article."""
        return [
            format_cv_item(item, article.get("language", ""))
            for item in article.get("place", [])
        ]

    #  Updated Code here to fetch Events from Article
    def _get_event(self, article):
        """Get event list for article."""
        return [
            format_cv_item(item, article.get("language", ""))
            for item in article.get("event", [])
        ]

    #  Updated Code here to fetch Person from Article
    def _get_person(self, article):
        """Get person list for article."""
        return [
            format_cv_item(item, article.get("language", ""))
            for item in article.get("person", [])
        ]

    def _get_service(self, article):
        """Get service list for article.

        It's using `anpa_category` to populate service field for now.
        """
        return [
            format_cv_item(item, article.get("language", ""))
            for item in article.get("anpa_category", [])
        ]

    def _get_renditions(self, article):
        """Get renditions for article."""
        # get the actual article's renditions
        actual_renditions = article.get("renditions", {})
        # renditions list that we want to publish
        if article["type"] == "picture":
            renditions_to_publish = self.internal_renditions + list(
                get_renditions_spec(without_internal_renditions=True).keys()
            )
            # filter renditions and keep only the ones we want to publish
            actual_renditions = {
                name: actual_renditions[name]
                for name in renditions_to_publish
                if name in actual_renditions
            }
        # format renditions to Ninjs
        renditions = {}
        for name, rendition in actual_renditions.items():
            if rendition:
                renditions[name] = self._format_rendition(rendition)
        return renditions

    def _format_rendition(self, rendition):
        """Format single rendition using fields whitelist."""
        formatted = {}
        for field in self.rendition_properties:
            if field not in rendition:
                continue
            formatted[field] = rendition[field]
            if field in ("width", "height"):
                if rendition.get(field):
                    formatted[field] = int(rendition[field])
                else:
                    formatted.pop(field, None)
        return formatted

    def _format_place(self, article):
        vocabularies_service = superdesk.get_resource_service("vocabularies")
        locator_map = vocabularies_service.find_one(req=None, _id="locators")
        if locator_map and "items" in locator_map:
            locator_map["items"] = vocabularies_service.get_locale_vocabulary(
                locator_map.get("items"), article.get("language")
            )

        def get_label(item):
            if locator_map:
                locators = [
                    loc
                    for loc in locator_map.get("items", [])
                    if loc["qcode"] == item.get("qcode")
                ]
                if locators and len(locators) == 1:
                    return (
                        locators[0].get("state")
                        or locators[0].get("country")
                        or locators[0].get("world_region")
                        or locators[0].get("group")
                    )
            return item.get("name")

        places = []
        for item in article.get("place", []):
            if item.get("scheme") == "geonames":
                places.append(self._format_geonames(item))
            else:
                if config.NINJS_PLACE_EXTENDED:
                    place = {}
                    for key in item.keys():
                        if item.get(key):
                            if key == "qcode":
                                place["code"] = item.get(key)
                            elif key == "name":
                                if get_label(item) is not None:
                                    place["name"] = get_label(item)
                                else:
                                    place["name"] = item.get(key)
                            else:
                                place[key] = item.get(key)
                else:
                    place = {"name": get_label(item), "code": item.get("qcode")}
                places.append(place)
        return places

    def _format_geonames(self, place):
        fields = ["scheme", "code", "name"]
        if app.config.get("NINJS_PLACE_EXTENDED"):
            fields.extend(
                [
                    "state",
                    "state_code",
                    "country",
                    "country_code",
                ]
            )
        geo = {k: v for k, v in place.items() if k in fields}
        if app.config.get("NINJS_PLACE_EXTENDED") and place.get("location"):
            geo["geometry_point"] = {
                "type": "Point",
                "coordinates": [
                    place["location"].get("lat"),
                    place["location"].get("lon"),
                ],
            }
        return geo

    def _format_profile(self, profile):
        return superdesk.get_resource_service("content_types").get_output_name(profile)

    def _format_signal_cwarn(self):
        return [
            {"name": "Content Warning", "code": "cwarn", "scheme": SCHEME_MAP["sig"]}
        ]

    def _format_attachments(self, article):
        output = []
        attachments_service = superdesk.get_resource_service("attachments")
        for attachment_ref in article["attachments"]:
            attachment = attachments_service.find_one(
                req=None, _id=attachment_ref["attachment"]
            )
            href = get_attachment_public_url(attachment)
            if href:
                # If we get a href, the attachment is available for subscriber consumption
                output.append(
                    {
                        "id": str(attachment["_id"]),
                        "title": attachment["title"],
                        "description": attachment.get("description"),
                        "filename": attachment["filename"],
                        "mimetype": attachment["mimetype"],
                        "length": attachment.get("length"),
                        "media": str(attachment["media"]),
                        "href": href,
                    }
                )
        return output

    def _format_authors(self, article):
        users_service = superdesk.get_resource_service("users")
        vocabularies_service = superdesk.get_resource_service("vocabularies")
        job_titles_voc = vocabularies_service.find_one(None, _id="job_titles")
        if job_titles_voc and "items" in job_titles_voc:
            job_titles_voc["items"] = vocabularies_service.get_locale_vocabulary(
                job_titles_voc.get("items"), article.get("language")
            )
        job_titles_map = (
            {v["qcode"]: v["name"] for v in job_titles_voc["items"]}
            if job_titles_voc is not None
            else {}
        )

        authors = []
        for author in article["authors"]:
            try:
                user_id = author["parent"]
            except KeyError:
                # XXX: in some older items, parent may be missing, we try to find user with name in this case
                try:
                    user = next(users_service.find({"display_name": author["name"]}))
                except (StopIteration, KeyError):
                    logger.warning("unknown user")
                    user = {}
            else:
                try:
                    user = next(users_service.find({"_id": user_id}))
                except StopIteration:
                    logger.warning("unknown user: {user_id}".format(user_id=user_id))
                    user = {}

            avatar_url = user.get("picture_url", author.get("avatar_url"))

            author = {
                "code": str(user.get("_id", author.get("name", ""))),
                "name": user.get("display_name", author.get("name", "")),
                "role": author.get("role", ""),
                "biography": user.get("biography", author.get("biography", "")),
            }

            # include socials only if they are non-empty
            socials = ("facebook", "twitter", "instagram")
            for social in socials:
                social_data = user.get(social, author.get(social, ""))
                if social_data:
                    author[social] = social_data

            if avatar_url:
                author["avatar_url"] = avatar_url

            job_title_qcode = user.get("job_title")
            if job_title_qcode is not None:
                author["jobtitle"] = {
                    "qcode": job_title_qcode,
                    "name": job_titles_map.get(job_title_qcode, ""),
                }

            authors.append(author)
        return authors

    def _format_signal(self, signal):
        scheme, code = signal["qcode"].split(":")
        return dict(
            name=signal["name"],
            code=code,
            scheme=SCHEME_MAP.get(scheme) or scheme,
        )


class NINJS2Formatter(NINJSFormatter_2):
    """NINJS formatter v2

    .. versionadded:: 2.0

    Extending :py:class:`NINJSFormatter_2` to avoid breaking changes.

    *Changes*:

    - user ``correction_sequence`` for ``version`` field, so it's 1, 2, 3, ... in the output
    - add ``rewrite_sequence`` field
    - add ``rewrite_of`` field

    """

    name = "NINJSv3"
    type = "ninjs3"

    direct_copy_properties = NINJSFormatter_2.direct_copy_properties + (
        "rewrite_sequence",
        "rewrite_of",
    )

    def _transform_to_ninjs(self, article, subscriber, recursive=True):
        ninjs = super()._transform_to_ninjs(article, subscriber, recursive)

        ninjs["version"] = str(article.get("correction_sequence", 1))

        return ninjs
