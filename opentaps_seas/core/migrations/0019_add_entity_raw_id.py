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

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_add_tags_to_views'),
    ]

    operations = [
        migrations.RunSQL(
            """
            DROP VIEW IF EXISTS core_site_view;
            CREATE OR REPLACE VIEW core_site_view AS
                SELECT entity_id,
                kv_tags->'id' as object_id,
                kv_tags->'dis' as description,
                kv_tags->'geoState' as state,
                kv_tags->'geoCity' as city,
                kv_tags->'area' as area,
                kv_tags,
                m_tags
                FROM core_entity
                WHERE 'site' = ANY(m_tags);
            """,
            reverse_sql="""
            DROP VIEW IF EXISTS core_site_view;
            CREATE OR REPLACE VIEW core_site_view AS
                SELECT entity_id,
                kv_tags->'dis' as description,
                kv_tags->'geoState' as state,
                kv_tags->'geoCity' as city,
                kv_tags->'area' as area,
                kv_tags,
                m_tags
                FROM core_entity
                WHERE 'site' = ANY(m_tags);
            """
        ),
        migrations.RunSQL(
            """
            DROP VIEW IF EXISTS core_equipment_view;
            CREATE OR REPLACE VIEW core_equipment_view AS
                SELECT entity_id,
                kv_tags->'id' as object_id,
                kv_tags->'dis' as description,
                kv_tags->'siteRef' as site_id,
                kv_tags,
                m_tags
                FROM core_entity
                WHERE 'equip' = ANY(m_tags);
            """,
            reverse_sql="""
            DROP VIEW IF EXISTS core_equipment_view;
            CREATE OR REPLACE VIEW core_equipment_view AS
                SELECT entity_id,
                kv_tags->'dis' as description,
                kv_tags->'siteRef' as site_id,
                kv_tags,
                m_tags
                FROM core_entity
                WHERE 'equip' = ANY(m_tags);
            """
        ),
        migrations.RunSQL(
            """
            DROP VIEW IF EXISTS core_entity_view;
            CREATE OR REPLACE VIEW core_entity_view AS
                SELECT entity_id,
                kv_tags->'id' as object_id,
                kv_tags->'dis' as description,
                kv_tags,
                m_tags
                FROM core_entity;
            """,
            reverse_sql="""
            DROP VIEW IF EXISTS core_entity_view;
            CREATE OR REPLACE VIEW core_entity_view AS
                SELECT entity_id,
                kv_tags->'dis' as description,
                kv_tags,
                m_tags
                FROM core_entity;
            """
        ),
        migrations.RunSQL(
            """
            DROP VIEW IF EXISTS core_model_view;
            CREATE OR REPLACE VIEW core_model_view AS
                SELECT entity_id,
                kv_tags->'id' as object_id,
                kv_tags->'dis' as description,
                kv_tags,
                m_tags
                FROM core_entity
                WHERE 'model' = ANY(m_tags);
            """,
            reverse_sql="""
            DROP VIEW IF EXISTS core_model_view;
            CREATE OR REPLACE VIEW core_model_view AS
                SELECT entity_id,
                kv_tags->'dis' as description,
                kv_tags,
                m_tags
                FROM core_entity
                WHERE 'model' = ANY(m_tags);
            """,
        ),
        migrations.RunSQL(
            """
            DROP VIEW IF EXISTS core_point_view;
            CREATE OR REPLACE VIEW core_point_view AS
                SELECT entity_id,
                topic,
                'N/A' as current_value,
                kv_tags->'id' as object_id,
                kv_tags->'dis' as description,
                kv_tags->'kind' as kind,
                kv_tags->'unit' as unit,
                kv_tags->'siteRef' as site_id,
                kv_tags->'equipRef' as equipment_id,
                dashboard_uid,
                bacnet_fields,
                kv_tags,
                m_tags
                FROM core_entity
                WHERE 'point' = ANY(m_tags);
            """,
            reverse_sql="""
            DROP VIEW IF EXISTS core_point_view;
            CREATE OR REPLACE VIEW core_point_view AS
                SELECT entity_id,
                topic,
                'N/A' as current_value,
                kv_tags->'dis' as description,
                kv_tags->'kind' as kind,
                kv_tags->'unit' as unit,
                kv_tags->'siteRef' as site_id,
                kv_tags->'equipRef' as equipment_id,
                dashboard_uid,
                bacnet_fields,
                kv_tags,
                m_tags
                FROM core_entity
                WHERE 'point' = ANY(m_tags);
            """,
        )
    ]
