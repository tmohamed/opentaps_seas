# This file is part of opentaps Smart Energy Applications Suite (SEAS).

# opentaps Smart Energy Applications Suite (SEAS) is free software:
# you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# opentaps Smart Energy Applications Suite (SEAS) is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with opentaps Smart Energy Applications Suite (SEAS).
# If not, see <https://www.gnu.org/licenses/>.

import logging
import eeweather
import geocoder
import hashlib
import json
import numpy
import pytz
import requests
import re
from math import isnan
from .models import Entity
from .models import EquipmentView
from .models import PointView
from .models import Tag
from .models import TimeZone
from .models import Topic
from .models import Meter
from .models import MeterFinancialValue
from .models import MeterProduction
from .models import MeterRatePlan
from .models import ModelView
from .models import WeatherHistory
from .models import WeatherStation
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pytz import all_timezones
from pytz import UnknownTimeZoneError
from pytz import timezone as pytz_timezone
from dateutil.parser import parse as parse_datetime
from django.db import connections
from django.db import OperationalError
from django.db.models import Q
from django.urls import reverse
from django.utils.html import format_html
from django.conf import settings
from django.utils.crypto import get_random_string
from django.utils.text import slugify

logger = logging.getLogger(__name__)


def parse_timezone(tzname, default=None):
    if tzname:
        # accept valid pytz names like US/Pacific
        try:
            return pytz_timezone(tzname)
        except UnknownTimeZoneError:
            pass
        # else check if it is a short name
        for zone in all_timezones:
            if tzname == zone.split('/')[-1]:
                return pytz_timezone(zone)
        # else check if it is one of out Timezone
        try:
            tz = TimeZone.objects.get(time_zone=tzname)
            return timezone(timedelta(seconds=tz.tzoffset))
        except TimeZone.DoesNotExist:
            pass

        logging.error('Unknown timezone {}'.format(tzname))
    if default:
        return pytz_timezone(default)
    return None


def format_epoch(ts):
    if hasattr(ts, 'timestamp'):
        ts = round(ts.timestamp() * 1000)
    t = datetime.utcfromtimestamp(ts // 1000).replace(microsecond=ts % 1000 * 1000).replace(tzinfo=timezone.utc)
    time = t.isoformat()
    fmttime = t.strftime("%m/%d/%Y %H:%M:%S")
    return {'epoch': ts, 'fmttime': fmttime, 'time': time}


def get_current_value_dict(point):
    result = None
    with connections['crate'].cursor() as c:
        sql = """SELECT ts, string_value FROM "data" WHERE topic = %s ORDER BY ts DESC LIMIT 1;"""
        c.execute(sql, [point.topic])
        for record in c:
            result = record

    if not result:
        return None
    ts = result[0]
    string_value = result[1]
    epoch = int(ts.timestamp() * 1000)
    t = format_epoch(epoch)
    time = t['time']
    fmttime = t['fmttime']
    if point.kind == 'Bool':
        return {'value': string_value == 't' or string_value != '0', 'fmttime': fmttime, 'time': time, 'epoch': epoch}
    else:
        if point.kind == 'Number' and (point.unit == '°F' or point.unit == '°C'):
            try:
                v = float(string_value)
                # use 1 after the dot for temperatures
                string_value = format(v, '.1f')
            except Exception:
                pass
        if point.unit:
            return {'value': string_value, 'epoch': epoch, 'fmttime': fmttime, 'time': time, 'unit': point.unit}
        else:
            if point.kind == 'Number' and (point.unit == '°F' or point.unit == '°C'):
                try:
                    v = float(string_value)
                    # use 1 after the dot for temperatures
                    string_value = format(v, '.1f')
                except Exception:
                    pass
            if point.unit:
                return {'value': string_value, 'epoch': epoch, 'fmttime': fmttime, 'time': time, 'unit': point.unit}
            else:
                return {'value': string_value, 'epoch': epoch, 'fmttime': fmttime, 'time': time}


def get_current_value(point, raw=False):
    d = get_current_value_dict(point)
    if not d:
        return None

    logger.info("get_current_value %s -- %s", point, d)
    value = d['value']
    if raw:
        return d
    if isinstance(value, str) and '{' in value:
        value = value.replace('{', '').replace('}', '')
    if point.kind == 'Bool':
        return format_html('<b>%s</b> as of <my-datetime value="%s">%s</my-datetime>' %
                           (value, d['time'], d['fmttime']), )
    else:
        if point.unit:
            return format_html('<b>%s</b> %s as of <my-datetime value="%s">%s</my-datetime>' %
                               (value, d['unit'], d['time'], d['fmttime']), )
        else:
            return format_html('<b>%s</b> as of <my-datetime value="%s">%s</my-datetime>' %
                               (value, d['time'], d['fmttime']), )


def add_current_values(data, raw=False):
    d2 = list(data)
    for d in d2:
        cv = None
        try:
            cv = get_current_value(d, raw=raw)
        except OperationalError:
            logging.warning('Crate database unavailable')
        if not cv:
            if raw:
                cv = {}
            else:
                cv = 'N/A'
        d.current_value = cv
    return d2


def get_ahu_current_values(equipment_id):
    # this find the data points and current values for:
    # Space Air Temp:  {air,his,point,zone,sensor,temp}
    # Return Air Temp: {air,his,point,return,sensor,temp}
    # Supply Fan Speed - {air,discharge,fan,his,point,sensor,speed}
    # Cooling -  {cooling,his,point,sensor}
    # Heating - {heat,his,point,sensor}
    # CO2 - {co2,his,point,sensor,zone}
    # -> those are returned in a dictionary 'Point Name': {<current_data>}
    q = {
        'Space Air Temp': {
            'has': ['air', 'his', 'point', 'zone', 'sensor', 'temp'],
            'exclude': ['mixed', 'discharge', 'return', 'outside']
        },
        'Return Air Temp': {'has': ['air', 'his', 'point', 'sensor', 'temp', 'return']},
        'Supply Fan Speed': {'has': ['air', 'his', 'point', 'sensor', 'fan', 'speed', 'discharge']},
        'Cooling': {
            'has': ['his', 'point', 'sensor', 'cooling'],
            'exclude': ['heat']
        },
        'Heating': {
            'has': ['his', 'point', 'sensor', 'heat'],
            'exclude': ['cooling']
        },
        'CO2': {'has': ['his', 'point', 'sensor', 'co2', 'zone']}
    }
    results = {}
    data_points = PointView.objects.filter(equipment_id=equipment_id)
    for n, t in q.items():
        p = data_points.filter(m_tags__contains=t['has'])
        if 'exclude' in t:
            p = p.exclude(m_tags__overlap=t['exclude'])
        if p and len(p) > 0:
            if len(p) > 1:
                # some like space air temp tags also match return air temp .. assume the less amount of tags
                pl = list(p)
                pl.sort(key=lambda x: len(x.m_tags))
                logger.warning('get_ahu_current_values for %s has more than one point: %s', equipment_id, pl)
                for cp in pl:
                    logger.warning('--> %s with tag %s', cp.topic, cp.m_tags)
                p = pl
            p = p[0]
            logger.warning('get_ahu_current_values using point: %s', p)
            results[n] = add_current_values([p], raw=True)[0]
    return results


DEFAULT_RANGE = '24h'
DEFAULT_RES = 'minute'


def get_start_date_from_range(trange, from_datetime=None):
    if not trange:
        trange = DEFAULT_RANGE

    # convert the range
    if not from_datetime:
        from_datetime = datetime.utcnow()
    end = from_datetime
    start = from_datetime
    if 'today' == trange:
        start -= timedelta(days=1)
    elif 'yesterday' == trange:
        start -= timedelta(days=2)
        end -= timedelta(days=1)
    elif len(trange) > 7 and ' months' == trange[-7:]:
        start -= timedelta(days=30 * int(trange[:-7]))
    elif len(trange) > 6 and ' month' == trange[-6:]:
        start -= timedelta(days=30 * int(trange[:-6]))
    elif len(trange) > 6 and ' years' == trange[-6:]:
        start -= timedelta(days=365 * int(trange[:-6]))
    elif len(trange) > 5 and ' year' == trange[-5:]:
        start -= timedelta(days=365 * int(trange[:-5]))
    elif len(trange) > 5 and ' days' == trange[-5:]:
        start -= timedelta(days=int(trange[:-5]))
    elif len(trange) > 1 and 'h' == trange[-1:]:
        start -= timedelta(hours=int(trange[:-1]))
    elif len(trange) > 1 and 'm' == trange[-1:]:
        start -= timedelta(minutes=int(trange[:-1]))
    else:
        # can be given as <date> or <date>,<date>
        dr = trange.split(',')
        start = parse_datetime(dr[0])
        if len(dr) > 1:
            end = parse_datetime(dr[1])
    return start, end


def get_point_values(d, date_trunc=DEFAULT_RES, value_func='avg', trange=DEFAULT_RANGE, ts_as_datetime=False):
    # validate the date_trunc
    if date_trunc not in ['day', 'hour', 'minute', 'second']:
        date_trunc = DEFAULT_RES
    # use different queries for Number type sensors
    is_number = 'Number' == d.kind
    is_bool = 'Bool' == d.kind

    start, end = get_start_date_from_range(trange)

    logger.info("Getting data points for range %s -- %s", start, end)

    if is_number:
        sql = """SELECT DATE_TRUNC('{}', ts) as timest, {}(double_value) FROM "data"
                 WHERE topic = %s AND ts > %s AND ts <= %s
                 GROUP BY timest ORDER BY timest DESC;""".format(date_trunc, value_func)
    elif is_bool:
        # use MIN as function since we query string_value
        sql = """SELECT DATE_TRUNC('{}', ts) as timest, MIN(string_value), {}(double_value) FROM "data"
                 WHERE topic = %s AND ts > %s AND ts <= %s
                 GROUP BY timest ORDER BY timest DESC;""".format(date_trunc, value_func)
    else:
        sql = """SELECT ts, string_value FROM "data"
                 WHERE topic = %s AND ts > %s AND ts <= %s ORDER BY ts DESC;"""

    data = []
    with connections['crate'].cursor() as cursor:
        cursor.execute(sql, [d.topic, start, end])
        while True:
            result = cursor.fetchone()
            if result is None:
                break
            ts = result[0]
            value = result[1]
            if is_bool:
                if value and (value == 't' or value != '0'):
                    value = 1
                else:
                    value = round(result[2])
            if ts_as_datetime:
                # convert from epoch to datetime directly
                ts = datetime.utcfromtimestamp(ts // 1000).replace(microsecond=0).replace(tzinfo=timezone.utc)
            data.append([ts, value])
        logger.info("Got %s data points for %s", len(data), d.entity_id)
        cursor.close()
    return list(reversed(data))


def get_topics_tags_report():
    topics = Topic.objects.all().order_by('topic')
    report_rows = []
    report_header = []
    topics_tags = {}

    report_header, topics_tags = get_topics_tags_report_header()

    # prepare report rows
    for topic in topics:
        topic_tags = topics_tags.get(topic.topic)
        row = [topic.topic]
        if not topic_tags:
            row.extend([''] * len(report_header))
        else:
            kv_tags = topic_tags.get("kv_tags", {})
            m_tags = topic_tags.get("m_tags", [])
            if kv_tags or m_tags:
                for tag in report_header:
                    if kv_tags and tag in kv_tags.keys():
                        value = kv_tags[tag]
                        row.append(value)
                    elif m_tags and tag in m_tags:
                        row.append("X")
                    else:
                        row.append("")
            else:
                row.append("")

        report_rows.append(row)

    report_header.insert(0, "__topic")
    return report_rows, report_header


def get_topics_tags_report_header():
    points = PointView.objects.all().order_by('topic')
    report_header = []
    topics_tags = {}

    for point in points:
        tags_exists = False
        topic_tags = {}
        if point.kv_tags:
            topic_tags["kv_tags"] = point.kv_tags
            tags_exists = True
            for key in point.kv_tags.keys():
                if key not in report_header:
                    report_header.append(key)

        if point.m_tags:
            topic_tags["m_tags"] = point.m_tags
            tags_exists = True
            for tag in point.m_tags:
                if tag not in report_header:
                    report_header.append(tag)

        if tags_exists:
            topics_tags[point.topic] = topic_tags
    report_header = sorted(report_header)

    return report_header, topics_tags


def tag_rulesets_run_report(entities):
    report_rows = []
    report_header = []
    topics_tags = {}

    report_header, topics_tags = get_topics_tags_report_header()

    for key in sorted(entities.keys()):
        entity = entities[key]
        row = [key]
        if not entity:
            row.extend([''] * len(report_header))
        else:
            kv_tags = entity.get('kv_tags')
            m_tags = entity.get('m_tags')
            if kv_tags or m_tags:
                for tag in report_header:
                    if kv_tags and tag in kv_tags.keys():
                        value = kv_tags[tag]
                        row.append(value)
                    elif m_tags and tag in m_tags:
                        row.append("X")
                    else:
                        row.append("")
            else:
                row.append("")

        report_rows.append(row)

    report_header.insert(0, "__topic")
    return report_rows, report_header


def tag_rulesets_run_report_diff(entities, updated_tags, removed_tags):
    report_rows = []
    report_header = []
    report_header_diff = []
    topics_tags = {}

    if updated_tags or removed_tags:
        report_header_all, topics_tags = get_topics_tags_report_header()
        for key in updated_tags.keys():
            tags = updated_tags.get(key)
            for tag in report_header_all:
                if tag in tags.keys() and tag not in report_header:
                    report_header.append(tag)
        for key in removed_tags.keys():
            tags = removed_tags.get(key)
            for tag in report_header_all:
                if tag in tags.keys() and tag not in report_header:
                    report_header.append(tag)

        report_header = sorted(report_header)
        for tag in report_header:
            report_header_diff.append(tag + ' previous')
            report_header_diff.append(tag + ' new')

        for key in sorted(entities.keys()):
            entity = entities[key]
            removed = removed_tags.get(key, {})
            updated = updated_tags.get(key, {})
            row = []
            if entity:
                if removed or updated:
                    for tag in report_header:
                        previous = ''
                        new = ''
                        if tag in removed.keys():
                            previous = removed.get(tag, '')
                        elif tag in updated.keys():
                            value_item = updated.get(tag, '')
                            if value_item:
                                previous = value_item.get('previous', '')
                                new = value_item.get('new', '')

                        if previous == 'type:MARKER':
                            previous = 'X'
                        if new == 'type:MARKER':
                            new = 'X'
                        if previous is None:
                            previous = ''
                        if new is None:
                            new = ''
                        row.append(previous)
                        row.append(new)

                if row:
                    row.insert(0, key)
                    report_rows.append(row)

    if report_header_diff:
        report_header_diff.insert(0, "__topic")
    return report_rows, report_header_diff


def charts_for_points(points):
    charts = []
    i = 0
    for point in points:
        data = get_current_value_dict(point)
        # skip empty charts
        if data:
            i += 1
            title = point.description
            unit = ''
            if point.unit:
                unit = point.unit
                title = '{} {}'.format(title, point.unit)
            if 'Bool' == point.kind:
                unit = 'on/off'
            charts.append({
                'index': i,
                'title': title,
                'unit': unit,
                'site_id': point.site_id,
                'equipment_id': point.equipment_id,
                'point_id': point.entity_id,
                'point': point,
                'current_value': data,
                'url': reverse("core:point_data_json", kwargs={"point": point.entity_id}),
                'isBool': point.kind == 'Bool'
            })
    return charts


def get_tag(tag):
    try:
        return Tag.objects.raw(
            'SELECT tag, description, details, kind FROM {0} WHERE tag = %s'.format(Tag._meta.db_table), [tag])[0]
    except IndexError:
        return None


def get_tag_description(tag, default=None):
    tag_row = get_tag(tag)
    if tag_row and tag_row.description:
        return tag_row.description
    else:
        return default


def get_tags_list(tags):
    tags_list = []
    for tag in tags:
        if not tag.kv_tags:
            continue
        for key in tag.kv_tags:
            tag_row = get_tag(key)
            description = ""
            details = ""
            kind = ""
            if tag_row:
                description = tag_row.description
                details = tag_row.details
                kind = tag_row.kind
            else:
                description = key

            t = {
                "tag": key,
                "value": tag.kv_tags[key],
                "details": details,
                "kind": kind,
                "description": description
            }
            if kind == 'Ref' and t['value']:
                # validate the linked entity
                entity = Tag.get_ref_entity_for(key, t['value'])
                if entity:
                    t['slug'] = entity.entity_id
            tags_list.append(t)

    for tag in tags:
        if not tag.m_tags:
            continue
        for tag in tag.m_tags:
            tag_row = get_tag(tag)
            description = ""
            details = ""
            if tag_row:
                description = tag_row.description
                details = tag_row.details
            else:
                description = tag

            tags_list.append({
                "tag": tag,
                "details": details,
                "description": description})
    return tags_list


def get_mtags_for_topic(entity_id):
    return Entity.objects.raw('''SELECT
        entity_id,
        kv_tags
        FROM {0} WHERE entity_id = %s'''.format(Entity._meta.db_table), [entity_id])


def get_tags_for_topic(entity_id):
    return Entity.objects.raw('''SELECT
        entity_id,
        kv_tags,
        m_tags
        FROM {0} WHERE entity_id = %s'''.format(Entity._meta.db_table), [entity_id])


def get_tags_list_for_topic(entity_id):
    return get_tags_list(get_tags_for_topic(entity_id))


def get_related_model_id(entity_id):
    tags = get_tags_for_topic(entity_id)
    for tag in tags:
        try:
            if tag.kv_tags and tag.kv_tags['modelRef']:
                return tag.kv_tags['modelRef']
        except KeyError:
            continue
    return None


def format_date(dt):
    if not dt:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat()


def serialize_raw_queryset(qs):
    data = []
    for obj in qs:
        d = {}
        for f in obj._meta.fields:
            d[f.name] = getattr(obj, f.name)
        data.append(d)
    return data


def create_grafana_dashboard(topic):
    logger.info('create_grafana_dashboard : %s', topic)
    auth = (settings.GRAFANA_USER_NAME, settings.GRAFANA_USER_PASSWORD)
    url = settings.GRAFANA_BASE_URL + "/api/dashboards/db"
    template_file = str(settings.ROOT_DIR.path('')) + "/data/dashboard/point-dashboard.json"
    f = open(template_file, 'r')
    datastore = json.load(f)
    title = datastore["dashboard"]["title"]
    panel_title = datastore["dashboard"]["panels"][0]["title"]
    raw_sql = datastore["dashboard"]["panels"][0]["targets"][0]["rawSql"]

    datastore["dashboard"]["title"] = title.replace("${pointName}", topic)
    datastore["dashboard"]["panels"][0]["title"] = panel_title.replace("${pointName}", topic)
    datastore["dashboard"]["panels"][0]["targets"][0]["rawSql"] = raw_sql.replace("${pointName}", topic)

    try:
        r = requests.post(url, verify=False, json=datastore, auth=auth)
    except requests.exceptions.ConnectionError:
        logger.exception('Could not create the grafan dashboard')
        return None

    logger.info('create_grafana_dashboard done : %s', r)
    return r


def create_equipment_grafana_dashboard(topic, equipmen_ref):
    logger.info('create_equipment_grafana_dashboard : %s', topic)
    auth = (settings.GRAFANA_USER_NAME, settings.GRAFANA_USER_PASSWORD)
    url = settings.GRAFANA_BASE_URL + "/api/dashboards/db"
    template_file = str(settings.ROOT_DIR.path('')) + "/data/dashboard/ahu-dashboard.json"
    f = open(template_file, 'r')
    datastore = json.load(f)
    title = datastore["dashboard"]["title"]
    panel_title = datastore["dashboard"]["panels"][0]["title"]
    targets = datastore["dashboard"]["panels"][0]["targets"]
    target_item = targets[0]

    datastore["dashboard"]["title"] = title.replace("${equipmentName}", topic)
    datastore["dashboard"]["panels"][0]["title"] = panel_title.replace("${equipmentName}", topic)

    metrics = get_equipment_metrics(equipmen_ref)
    targets = []
    for i, metric in enumerate(metrics):
        target = target_item.copy()
        target["refId"] = "A" + str(i)
        raw_sql = target["rawSql"]
        raw_sql = raw_sql.replace("${topic}", metric["topic"])
        raw_sql = raw_sql.replace("${metric}", metric["metric"])
        target["rawSql"] = raw_sql

        targets.append(target)

    datastore["dashboard"]["panels"][0]["targets"] = targets

    try:
        result_dashboard = requests.post(url, verify=False, json=datastore, auth=auth)
    except requests.exceptions.ConnectionError:
        logger.exception('Could not create the grafan dashboard')
        return None
    else:
        try:
            result_shapshot = create_grafana_dashboard_snapshot(datastore)
        except requests.exceptions.ConnectionError:
            result_shapshot = None

    logger.info('create_equipment_grafana_dashboard done : %s, %s', result_dashboard, result_shapshot)
    return result_dashboard, result_shapshot


def create_grafana_dashboard_snapshot(datastore):
    auth = (settings.GRAFANA_USER_NAME, settings.GRAFANA_USER_PASSWORD)
    url = settings.GRAFANA_BASE_URL + "/api/snapshots"

    datastore["dashboard"]["editable"] = False
    datastore["dashboard"]["hideControls"] = True
    datastore["dashboard"]["nav"] = [{"enable": False, "type": "timepicker"}]

    try:
        r = requests.post(url, verify=False, json=datastore, auth=auth)
    except requests.exceptions.ConnectionError:
        logger.exception('Could not create the grafan dashboard snapshot')
        return None

    logger.info('create_grafana_dashboard_snapshot done : %s', r)
    return r


def get_equipment_metrics(equipmen_ref):
    metrics = []
    conditions = []
    conditions.append({'metric': 'CoolValveCMD',
                       'm_tags': ['cooling', 'valve', 'cmd', 'his', 'point']
                       })
    conditions.append({'metric': 'HeatValveCmd',
                       'm_tags': ['heat', 'valve', 'cmd', 'his', 'point']
                       })
    conditions.append({'metric': 'OADamperCMD',
                       'm_tags': ['outside', 'air', 'damper', 'his', 'point']
                       })
    conditions.append({'metric': 'AvgZoneTemp',
                       'm_tags': ['temp', 'zone', 'air', 'his', 'point'],
                       'm_tags_exclude': ['sp']
                       })
    conditions.append({'metric': 'AvgZoneTempSP',
                       'm_tags': ['temp', 'zone', 'air', 'sp', 'his', 'point']
                       })
    conditions.append({'metric': 'MixedAirTemp',
                       'm_tags': ['temp', 'mixed', 'air', 'his', 'point']
                       })

    for condition in conditions:
        data_points = PointView.objects.filter(equipment_id=equipmen_ref)
        data_points = data_points.filter(m_tags__contains=condition['m_tags'])
        if 'm_tags_exclude' in condition:
            data_points = data_points.exclude(m_tags__overlap=condition['m_tags_exclude'])
        if data_points:
            for data_point in data_points:
                metric = {'metric': condition['metric'], 'topic': data_point.topic}
                metrics.append(metric)

    return metrics


def delete_grafana_dashboard(dashboard_uid):
    logger.info('delete_grafana_dashboard, dashboard_uid : %s', dashboard_uid)
    if dashboard_uid:
        auth = (settings.GRAFANA_USER_NAME, settings.GRAFANA_USER_PASSWORD)
        url = settings.GRAFANA_BASE_URL + "/api/dashboards/uid/" + dashboard_uid

        try:
            r = requests.delete(url, verify=False, auth=auth)
        except requests.exceptions.ConnectionError:
            logger.exception('Could not delete the grafan dashboard')
            return None

        logger.info('delete_grafana_dashboard result : %s', r)
        return r
    else:
        logger.error("dashboard_uid should not be empty")
        return None


def delete_grafana_dashboard_snapshot(dashboard_snapshot_uid):
    logger.info('delete_grafana_dashboard_snapshot, dashboard_snapshot_uid : %s', dashboard_snapshot_uid)
    if dashboard_snapshot_uid:
        auth = (settings.GRAFANA_USER_NAME, settings.GRAFANA_USER_PASSWORD)
        url = settings.GRAFANA_BASE_URL + "/api/snapshots/" + dashboard_snapshot_uid

        try:
            r = requests.delete(url, verify=False, auth=auth)
        except requests.exceptions.ConnectionError:
            logger.exception('Could not delete the grafan dashboard snapsho')
            return None

        logger.info('delete_grafana_dashboard_snapsho result : %s', r)
        return r
    else:
        logger.error("dashboard_snapshot_uid should not be empty")
        return None


def cleanup_id(id):
    if id:
        id = id.replace('/', '-')
    return id


def make_random_id(description, random_string_len=8):
    return slugify(description) + '-' + get_random_string(length=random_string_len)


def get_site_addr_loc(tags, site_id, session):
    show_google_map = False
    addr_loc = None
    site_address = ""
    allowed_tags = ['geoCity', 'geoCountry', 'geoPostalCode', 'geoState', 'geoStreet', 'geoAddr']
    geo_addr = None
    if settings.GOOGLE_API_KEY:
        if tags:
            for tag in tags:
                if tag in allowed_tags and tags[tag]:
                    if tag == 'geoAddr':
                        geo_addr = tags[tag]
                        continue
                    if site_address:
                        site_address = site_address + ", "
                    site_address = site_address + tags[tag]
                    if tag == 'geoStreet':
                        show_google_map = True

        if not show_google_map and geo_addr:
            if site_address:
                site_address = site_address + ", "
            site_address = site_address + geo_addr
        show_google_map = True

        if site_address and show_google_map:
            hash_object = hashlib.md5(site_address.encode('utf-8'))
            hash_key = hash_object.hexdigest()

            if hash_key in session:
                addr_loc = session[hash_key]

            if not addr_loc:
                geo = geocoder.google(site_address, key=settings.GOOGLE_API_KEY)

                if geo.json and geo.json["ok"]:
                    addr_loc = {"latitude": geo.json["lat"], "longitude": geo.json["lng"], "site_id": site_id}
                    session[hash_key] = addr_loc

    return addr_loc


def filter_Q(name, operator, value, valid_tags):
    logger.info('filter_Q %s %s %s', name, operator, value)
    # do a dynamic Q filter, for example:
    # kwargs = {
    #     '{0}__{1}'.format('name', 'startswith'): 'A',
    #     '{0}__{1}'.format('name', 'endswith'): 'Z'
    # }
    # Q(**kwargs)
    #
    # topic is the only direct field
    # for all others we check both in kv_tags and bacnet_fields
    # -> note: bacnet_fields are not availabe in the Crate entity yet
    # for present and absent we also check in m_tags (using the operator isnull)
    #
    if 'topic' == name:
        return Q(**{'topic__{0}'.format(operator): value})
    else:
        condition = Q()
        if name in valid_tags:
            condition.add(Q(**{'kv_tags__{0}__{1}'.format(name, operator): value}), Q.OR)
        else:
            # for NotEqual, NotContain, Absent we do not need to check the kv_tags since the
            # value does not exist at all
            # for Present this will just check the m_tags array
            # for Equal and Contain we already check in apply_filter_to_queryset
            pass
        if operator == 'isnull' and not value:
            # note this is only for operator isnull
            # note: only contains which is equivalent to isnull=False
            condition.add(Q(**{'m_tags__contains': [name]}), Q.OR)
        return condition


def apply_filter_to_queryset(qs, filter_field, filter_type, filter_value, valid_tags):
    logger.info('apply_filter_to_queryset: %s %s %s', filter_field, filter_type, filter_value)
    if not filter_field or filter_field == 'undefined' or filter_field == 'Topic':
        name = 'topic'
    else:
        name = filter_field
    if filter_type:
        if filter_type == 'present':
            qs = qs.filter(filter_Q(name, 'isnull', False, valid_tags))
        elif filter_type == 'absent':
            qs = qs.exclude(filter_Q(name, 'isnull', False, valid_tags))
        elif filter_value:
            # all of those test either topic OR a kv_tags
            # so fail if trying to match a kv_tag
            prefix = 'i'
            if not name == 'topic' and name not in valid_tags and (filter_type == 'c' or filter_type == 'eq'):
                logger.warning('topic filter found an unused tag: %s', name)
                return qs.none()
            if filter_type == 'c':
                qs = qs.filter(filter_Q(name, prefix + 'contains', filter_value, valid_tags))
            elif filter_type == 'nc':
                qs = qs.exclude(filter_Q(name, prefix + 'contains', filter_value, valid_tags))
            elif filter_type == 'eq':
                qs = qs.filter(filter_Q(name, prefix + 'exact', filter_value, valid_tags))
            elif filter_type == 'neq':
                qs = qs.exclude(filter_Q(name, prefix + 'exact', filter_value, valid_tags))
            elif filter_type == 'matches':
                qs = qs.filter(filter_Q(name, prefix + 'regex', filter_value, valid_tags))
            elif filter_type == 'gt':
                qs = qs.filter(filter_Q(name, 'gt', filter_value, valid_tags))
            elif filter_type == 'gte':
                qs = qs.filter(filter_Q(name, 'gte', filter_value, valid_tags))
            elif filter_type == 'lt':
                qs = qs.filter(filter_Q(name, 'lt', filter_value, valid_tags))
            elif filter_type == 'lte':
                qs = qs.filter(filter_Q(name, 'lte', filter_value, valid_tags))
    return qs


def apply_filters_to_queryset(qs, filters):
    # first we do a schema check since trying to fetch unused tags will cause a DB error
    valid_tags = []

    with connections['crate'].cursor() as c:
        sql = """SELECT column_name from information_schema.columns
                 WHERE table_name = 'topic' and column_name like 'kv_tags[%';"""
        c.execute(sql)
        for (cn, ) in c:
            # extract the tag name from "kv_tags['tag_name']"
            valid_tags.append(cn[9:-2])

    # ordering or AND and OR filters
    # A or B and C -> (A(qs) | B(qs)) & C(qs)
    # A or B and C or D -> (A(qs) | B(qs)) & (C(qs) | D(qs))
    # so we first resolve the ORs from the list:
    #  (A, *), (B, or), (C, and), (D, or)
    # The ORs apply to oqs (original qs)
    # then we AND each resulting qs
    oqs = qs
    first = True
    rqs = []
    for (filter_field, filter_type, filter_value, filter_op) in filters:
        logging.info('apply_filters_to_queryset: add filter %s', (filter_field, filter_type, filter_value, filter_op))
        if first:
            qs = apply_filter_to_queryset(oqs, filter_field, filter_type, filter_value, valid_tags)
        elif filter_op and filter_op.lower() == 'or':
            qs = qs | apply_filter_to_queryset(oqs, filter_field, filter_type, filter_value, valid_tags)
        else:
            # skip for now store the current qs
            rqs.append(qs)
            # reset working qs
            qs = oqs
            qs = apply_filter_to_queryset(oqs, filter_field, filter_type, filter_value, valid_tags)
        first = False

    # add the last qs
    rqs.append(qs)

    # now AND resulting qs
    qs = oqs
    for q in rqs:
        qs = qs & q

    return qs


def tag_topics(filters, tags, select_all=False, topics=[], select_not_mapped_topics=None, pretend=False):
    qs = Topic.objects.all()

    if select_not_mapped_topics:
        # only list topics where there is no related data point
        # because those are 2 different DB need to get all the data points
        # where topic is non null and remove those topics
        # note: cast topic into entity_id as raw query must have the model PK
        qs = qs.exclude(m_tags__contains=['point'])

    logging.info('tag_topics: using filters %s', filters)
    if filters:
        q_filters = []
        for qfilter in filters:
            filter_op = qfilter.get('o') or qfilter.get('op') or 'AND'
            filter_type = qfilter.get('t') or qfilter.get('type')
            filter_field = qfilter.get('n') or qfilter.get('field')
            if filter_type:
                filter_value = qfilter.get('f') or qfilter.get('value')
                q_filters.append((filter_field, filter_type, filter_value, filter_op))
        qs = apply_filters_to_queryset(qs, q_filters)

    # store a dict of topic -> data_point.entity_id
    updated = []
    updated_entities = {}
    updated_tags = {}
    removed_tags = {}
    for etopic in qs:
        topic = str(etopic.topic)
        if select_all or topic in topics:
            logging.info('tag_topics: apply to topic %s', topic)
            # update or create the Data Point
            try:
                e = Entity.objects.get(topic=topic)
            except Entity.DoesNotExist:
                entity_id = make_random_id(topic)
                e = Entity(entity_id=entity_id, topic=topic, m_tags=[])
                e.add_tag('id', entity_id, commit=False)
            if not e.kv_tags or not e.kv_tags.get('dis'):
                e.add_tag('dis', topic, commit=False)
            for tag in tags:
                # never tag with 'site' or 'equip'!
                if tag != 'site' and tag != 'equip':
                    if tag.get('remove') is True or tag.get('remove') == 'True':
                        logging.info('*** remove tag %s', tag)
                        if pretend:
                            current_tag = None
                            current_value = None
                            tag_tag = tag.get('tag')
                            if tag_tag in e.m_tags:
                                current_tag = tag_tag
                                current_value = 'type:MARKER'
                            else:
                                value = e.kv_tags.get(tag_tag)
                                if value:
                                    current_tag = tag_tag
                                    current_value = value

                            if current_tag:
                                tt = removed_tags.get(topic)
                                if not tt:
                                    tt = {}
                                tt[current_tag] = current_value

                                removed_tags[topic] = tt

                        e.remove_tag(tag.get('tag'), commit=False)
                    else:
                        logging.info('*** add tag %s', tag)
                        if pretend:
                            current_value = None
                            tag_tag = tag.get('tag')
                            if tag_tag in e.m_tags:
                                current_value = 'type:MARKER'
                            else:
                                value = e.kv_tags.get(tag_tag)
                                if value:
                                    current_value = value

                            tt = updated_tags.get(topic)
                            if not tt:
                                tt = {}
                            if tag.get('value'):
                                tt[tag.get('tag')] = {'new': tag.get('value'), 'previous': current_value}
                            else:
                                tt[tag.get('tag')] = {'new': 'type:MARKER', 'previous': current_value}

                            updated_tags[topic] = tt
                        e.add_tag(tag.get('tag'), value=tag.get('value'), commit=False)
            # if tagged with an equipRef make sure the siteRef also matches
            equip_ref = e.kv_tags.get('equipRef')
            if equip_ref:
                # let it fail if the equipment does not exist
                equip = EquipmentView.objects.get(object_id=equip_ref)
                if equip and equip.site_id:
                    e.add_tag('siteRef', equip.site_id, commit=False)
            if pretend:
                updated_entities[topic] = e
                logging.info('tag_topics: pretend changed %s %s %s', e, e.m_tags, e.kv_tags)
            else:
                e.save()
            updated.append({'topic': topic, 'point': e.entity_id, 'name': e.kv_tags.get('dis')})

    return updated, updated_entities, updated_tags, removed_tags


def get_bacnet_trending_data(rows):
    header = []
    bacnet_data = []
    bacnet_tags = [Tag.bacnet_tag_prefix + 'reference_point_name', Tag.bacnet_tag_prefix + 'volttron_point_name',
                   Tag.bacnet_tag_prefix + 'units', Tag.bacnet_tag_prefix + 'unit_details',
                   Tag.bacnet_tag_prefix + 'bacnet_object_type', Tag.bacnet_tag_prefix + 'property',
                   Tag.bacnet_tag_prefix + 'writable', Tag.bacnet_tag_prefix + 'index',
                   Tag.bacnet_tag_prefix + 'write_priority', Tag.bacnet_tag_prefix + 'notes']

    # make header
    for tag in bacnet_tags:
        header.append(get_tag_description(tag, default=tag.replace(Tag.bacnet_tag_prefix, '')))

    # make data
    for row in rows:
        data_row = []
        kv_tags = row.kv_tags
        if kv_tags:
            for i in range(0, len(bacnet_tags)):
                key = bacnet_tags[i]
                value = kv_tags.get(key)
                if value:
                    data_row.append(value)
                else:
                    data_row.append('')

        bacnet_data.append(data_row)

    return header, bacnet_data


def create_equipment_action(filters, action_fields):
    qs = Topic.objects.all()

    logging.info('create_equipment_action: using filters %s', filters)
    action_regexp_value = None
    new_equipments = {}
    if filters:
        q_filters = []
        for qfilter in filters:
            filter_type = qfilter.get('t') or qfilter.get('type')
            filter_field = qfilter.get('n') or qfilter.get('field')
            filter_op = qfilter.get('o') or qfilter.get('op')
            if filter_type:
                filter_value = qfilter.get('f') or qfilter.get('value')
                if not action_regexp_value and filter_type == 'matches':
                    if filter_value:
                        action_regexp_value = filter_value

                q_filters.append((filter_field, filter_type, filter_value, filter_op))
        qs = apply_filters_to_queryset(qs, q_filters)

        if action_fields and action_fields.get('equipment_name'):
            new_equipments_topics = {}
            for etopic in qs:
                stopic = str(etopic.topic)
                equipment_name = action_fields.get('equipment_name')
                if action_regexp_value:
                    m = re.match(action_regexp_value, stopic)
                    if m and len(m.groups()):
                        group = []
                        for i in range(len(m.groups()) + 1):
                            group.append(m.group(i))

                        try:
                            equipment_name = equipment_name.format(group=group)
                        except IndexError:
                            logging.error("cannot format equipment name string")

                if equipment_name not in new_equipments.keys():
                    current_equipment = create_equipment(equipment_name, action_fields)
                    new_equipments[equipment_name] = current_equipment

                equipment_topics = new_equipments_topics.get(equipment_name, [])
                equipment_topics.append(stopic)
                new_equipments_topics[equipment_name] = equipment_topics

            link_points_to_equipments(new_equipments, new_equipments_topics)

        else:
            logging.error("create_equipment_action: action equipment_name could not be empty")

    return new_equipments.keys()


def create_equipment(equipment_name, action_fields):
    entity_id = make_random_id(equipment_name)
    object_id = entity_id
    entity_id = slugify(entity_id)
    equipment = Entity(entity_id=entity_id)
    equipment.add_tag('equip', commit=False)
    equipment.add_tag('id', object_id, commit=False)
    equipment.add_tag('dis', equipment_name, commit=False)

    if action_fields.get('site_object_id'):
        equipment.add_tag('siteRef', action_fields.get('site_object_id'), commit=False)

    if action_fields.get('model_object_id'):
        equipment.add_tag('modelRef', action_fields.get('model_object_id'), commit=False)
        # add tags from model
        model_obj = ModelView.objects.filter(
            object_id=action_fields.get('model_object_id')).values().first()
        equipment.add_tags_from_model(model_obj, commit=False)

    equipment.save()

    return equipment


def link_points_to_equipments(new_equipments, new_equipments_topics):
    for equipment_name, equipment in new_equipments.items():
        topics = new_equipments_topics.get(equipment_name)
        for topic in topics:
            points = Entity.objects.filter(topic=topic)
            if points:
                for point in points:
                    point.add_tag('equipRef',  equipment.kv_tags['id'], commit=True)


def get_weather_station_for_location(latitude, longitude, as_object=True):
    ranked_stations = eeweather.rank_stations(latitude, longitude)
    for i in range(len(ranked_stations)):
        try:
            ranked_station = ranked_stations.iloc[i]
            if as_object:
                return WeatherStation.objects.get(weather_station_code=ranked_station.name)
            else:
                return ranked_station.name
        except Exception as e:
            logging.error(e)


def get_default_weather_station_for_site(site):
    if not site:
        return

    location = get_site_addr_loc(site.kv_tags, site.entity_id, {})
    if not location:
        return

    latitude = location.get('latitude')
    longitude = location.get('longitude')
    return get_weather_station_for_location(latitude, longitude)


def get_weather_history_for_station(weather_station, days_from_today=7, start_date=None, end_date=None):
    if days_from_today <= 0:
        days_from_today = 1     # 1 day as minimum
    if not end_date:
        end_date = datetime.now(pytz.UTC)
        logger.info('get_weather_history_for_station: using default now end_date %s', end_date)
    given_start = True
    if not start_date:
        start_date = end_date - timedelta(hours=days_from_today*24)
        logger.info('get_weather_history_for_station: using default start_date %s', start_date)
        given_start = False

    # Get last update datetime
    if not given_start:
        try:
            last_record = WeatherHistory.objects.filter(weather_station=weather_station).order_by('-as_of_datetime').first()
            if last_record:
                start_date = last_record.as_of_datetime + timedelta(minutes=1)
                logger.info('get_weather_history_for_station: last record start_date %s', start_date)
        except Exception as e:
            logging.warning(e)

    # Get weather station
    station = eeweather.ISDStation(weather_station.weather_station_code)

    # Make sure all dates are UTC
    start_date = start_date.replace(tzinfo=pytz.UTC)
    end_date = end_date.replace(tzinfo=pytz.UTC)
    logger.info('get_weather_history_for_station: from %s to %s',
                start_date, end_date)
    temp_degC, warnings = station.load_isd_hourly_temp_data(
                            start_date,
                            end_date,
                            read_from_cache=False)
    logger.info('get_weather_history_for_station: got %s / %s',
                temp_degC, warnings)
    for dt, deg_c in temp_degC.iteritems():
        if dt < start_date:
            continue
        elif numpy.isnan(deg_c):
            continue
        deg_f = deg_c * 9 / 5 + 32

        new_data = WeatherHistory(weather_station=weather_station, as_of_datetime=dt,
                                  temp_c=deg_c, temp_f=deg_f, source='EEWeather')
        new_data.save()

    return temp_degC


def get_topic_point(topic):
    point = None
    try:
        point = PointView.objects.filter(topic=topic)[0]
    except IndexError:
        point = None

    return point


def setup_sample_rate_plan(meter, price=0.2, from_datetime=None, calc_financials=False):
    # make the plan starting 2 years from now unless from_datetime is given
    if not from_datetime:
        from_datetime = datetime.utcnow()
        from_datetime -= timedelta(days=365*2)

    rp = MeterRatePlan.objects.create(
        description='Simple Rate Plan',
        from_datetime=from_datetime,
        billing_frequency_uom_id='time_interval_monthly',
        billing_day=1,
        params={
            'flat_rate': 0.2,
            'currency_uom_id': 'currency_USD',
            'energy_uom_id': 'energy_kWh'
            })

    if not meter.rate_plan:
        meter.rate_plan = rp
        meter.save()

    if calc_financials:
        calc_meter_financial_values(meter.meter_id, rp.rate_plan_id)

    return rp


def calc_meter_financial_values(meter_id, rate_plan_id, progress_observer=None):
    logger.info('calc_meter_financial_values: for Meter %s and Meter Rate Plan %s,', meter_id, rate_plan_id)

    plan = MeterRatePlan.objects.get(rate_plan_id=rate_plan_id)
    # (a) Iterate through a billing time period, for each interval:
    # (b) Calculate the billing of the original (counterfactual_usage) kWh and the actual (reporting_observed) kWh
    # (c) at the end of the period, store the billing period's from date and thru date, and then the actual
    #   billing - baseline model billing as the energy savings

    # Note it must NOT aggregate all the energy savings of the time period but rather iterate through the time period,
    #  because there are many billing schedules which are time interval sensitive: Different rates at different times
    #  of the day (cheap at 1500, expensive at 1700) or peak demand charges, which is a function of the highest kWh
    #  usage during a 15 minute interval of the billing period.

    # ensure meter exists
    meter = Meter.objects.get(meter_id=meter_id)

    # find the latest calculated financial value for this Meter
    last_value = MeterFinancialValue.objects.filter(meter_id=meter_id).order_by('thru_datetime').last()
    # find the interval of Meter PRoduction after that last financial value
    prod_qs = MeterProduction.objects.filter(meter_id=meter_id)
    if last_value:
        prod_qs = prod_qs.filter(thru_datetime__gt=last_value.thru_datetime)
    # filter by the plan valid period
    if plan.from_datetime:
        prod_qs = prod_qs.filter(from_datetime__gte=plan.from_datetime)
    if plan.thru_datetime:
        prod_qs = prod_qs.filter(thru_datetime__lt=plan.thru_datetime)

    if progress_observer:
        progress_observer.set_progress(1, prod_qs.count()+1, description='Calculating ...')

    results = []
    # get the meter production matching the time period
    # we want to group those into billing periods
    bp = None

    # for reference, save all attributes of the meter production entities here
    meter_production_reference = {}
    total_amount = 0.0

    # we need to iterate for each reference of production (different models, etc..)
    for ref in prod_qs.distinct('meter_production_reference').values('meter_production_reference'):
        ref = ref.get('meter_production_reference')
        qs = prod_qs.filter(meter_production_reference=ref)
        for prod in qs.order_by('thru_datetime'):
            # check if the billing period changed
            n_bp = plan.get_billing_period(prod.from_datetime)

            # first period
            if not bp:
                bp = n_bp

            if n_bp != bp:
                # we changed billing period, save the previous on if needed
                meter_production_reference['MeterRatePlan.id'] = plan.rate_plan_id
                m = MeterFinancialValue.objects.create(
                    meter=meter,
                    from_datetime=bp[0],
                    thru_datetime=bp[1],
                    source=prod.source,
                    meter_production_type=prod.meter_production_type,
                    meter_production_reference=meter_production_reference,
                    amount=total_amount,
                    uom=plan.currency_uom
                    )
                results.append(m)
                bp = n_bp
                meter_production_reference = {}
                total_amount = 0.0
            else:
                # same billing period update our current results
                val = plan.valuate_meter_production(prod)
                if isnan(val):
                    pass
                else:
                    total_amount += val
                    if prod.meter_production_reference:
                        meter_production_reference.update(prod.meter_production_reference)

            if progress_observer:
                progress_observer.add_progress()

    return results
