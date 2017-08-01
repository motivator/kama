from concurrent import futures
import argparse
import itertools
import functools
import grpc
import kama_pb2
import kama_pb2_grpc
from google.protobuf.empty_pb2 import Empty
import time
import os

import kama.database
import kama.context


def entity_to_pb(context, entity):
    pb = kama_pb2.Entity()
    pb.uuid = entity.uuid
    pb.name = entity.name
    pb.kind = entity.kind

    for attribute in entity.attributes(context=context):
        attribute_pb = pb.attributes.add()
        attribute_pb.uuid = attribute.uuid
        attribute_pb.entity.uuid = attribute.entity_uuid
        attribute_pb.entity.kind = entity.kind
        attribute_pb.entity.name = entity.name
        attribute_pb.key = attribute.key
        attribute_pb.value = attribute.value

    for link in entity.links_from(context=context):
        link_pb = pb.links_from.add()
        link_pb.uuid = link.uuid
        link_pb.from_entity.uuid = link.from_uuid
        link_pb.to_entity.uuid = link.to_uuid


    for link in entity.links_to(context=context):
        link_pb = pb.links_to.add()
        link_pb.uuid = link.uuid
        link_pb.from_entity.uuid = link.from_uuid
        link_pb.to_entity.uuid = link.to_uuid

    for permission in entity.permissions(context=context):
        permission_pb = pb.permissions.add()
        permission_pb.uuid = permission.uuid
        permission_pb.role.uuid = permission.role_uuid
        permission_pb.entity.uuid = permission.entity_uuid
        permission_pb.name = permission.name

    return pb


class DatabaseServicer(kama_pb2_grpc.KamaDatabaseServicer):
    def ListEntities(self, entity, context):
        request_context = kama.context.get_request_context(context)

        # Emulate the behavior of GetEntity. There can be only one.
        if entity.uuid:
            yield kama.database.Entity(entity.uuid)
            return
        if entity.name:
            yield kama.database.Entity.get_by_name(entity.name)
            return

        result = []
        filters = []

        if entity.kind:
            result = kama.database.Entity.get_by_kind(entity.kind)
        else:
            # This is inefficient. We're gonna get *all* the entities and
            # filter them
            result = kama.database.Entity.get_all()

        for attribute in entity.attributes:
            # Why are you searching by attribute uuid? You already know the entity at that point.
            assert not attribute.uuid
            if attribute.key and attribute.value:
                filters.append(lambda x: [y for y in x.attributes(key=attribute.key, context=request_context) if y.value == attribute.value])
            elif attribute.key:
                filters.append(lambda x: x.attributes(key=attribute.key, context=request_context))

        for link in entity.links_from:
            filters.append(lambda x: [y for y in x.links_from(context=request_context) if y.entity_to(context=request_context).uuid == link.to_entity.uuid])

        for link in entity.links_to:
            filters.append(lambda x: [y for y in x.links_to(context=request_context) if y.entity_from(context=request_context).uuid == link.from_entity.uuid])

        for permission in entity.permissions:
            if permission.uuid:
                filters.append(lambda x: [y for y in x.permissions(context=request_context) if y.uuid == permission.uuid])
            if permission.role:
                filters.append(lambda x: [y for y in x.permissions(context=request_context) if y.role.uuid == permission.role.uuid])
            if permission.entity:
                filters.append(lambda x: [y for y in x.permissions(context=request_context) if y.entity.uuid == permission.entity.uuid])
            if permission.name:
                filters.append(lambda x: [y for y in x.permissions(context=request_context) if y.name == permission.name])

        combo_filter = lambda x: all([bool(y(x)) for y in filters])
        for entity in result:
            try:
                if combo_filter(entity):
                    yield entity_to_pb(request_context, entity)
            except kama.database.PermissionDeniedException:
                pass

    def GetEntity(self, entity, context):
        request_context = kama.context.get_request_context(context)

        if entity.uuid:
            result = kama.database.Entity(entity.uuid)
        elif entity.kind and entity.name:
            result = kama.database.Entity.get_by_name(entity.kind, entity.name)

        if result:
            return entity_to_pb(request_context, result)
        else:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return entity

    def CreateEntity(self, request, context):
        request_context = kama.context.get_request_context(context)
        owner = kama.database.Entity(request.owner_role.uuid)

        if not owner in [x.from_entity() for x in request_context.user.links_to(context=request_context)]:
            # It's a bad idea to create entities owned by roles that you're not
            # a member of, because then you cannot get or modify the new
            # entity. We disallow this to prevent foot shooting.
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            return kama_pb2.Entity()

        entity = kama.database.Entity.create(request.entity.kind, request.entity.name, owner)
        return entity_to_pb(request_context, entity)

    def DeleteEntity(self, entity, context):
        request_context = kama.context.get_request_context(context)
        entity = kama.database.Entity(entity.uuid)
        entity.delete(context=request_context)
        return Empty()

    def UpdateEntity(self, entity, context):
        request_context = kama.context.get_request_context(context)
        db_entity = kama.database.Entity(entity.uuid)
        db_entity.set_name(entity.name, context=request_context)
        return entity_to_pb(request_context, db_entity)

    def AddAttribute(self, pb_attribute, context):
        request_context = kama.context.get_request_context(context)
        entity = kama.database.Entity(pb_attribute.entity.uuid)
        attribute = entity.add_attribute(key=pb_attribute.key, value=pb_attribute.value, context=request_context)
        pb_attribute.uuid = attribute.uuid
        return pb_attribute

    def DeleteAttributes(self, pb_attribute, context):
        request_context = kama.context.get_request_context(context)
        entity = kama.database.Entity(pb_attribute.entity.uuid)
        entity.delete_attributes(key=pb_attribute.key, context=request_context)
        return Empty()

    def AddLink(self, pb_link, context):
        request_context = kama.context.get_request_context(context)
        from_entity = kama.database.Entity(pb_link.from_entity.uuid)
        to_entity = kama.database.Entity(pb_link.to_entity.uuid)
        link = from_entity.add_link(to_entity, context=request_context)
        pb_link.uuid = link.uuid
        return pb_link

    def DeleteLink(self, pb_link, context):
        request_context = kama.context.get_request_context(context)
        from_entity = kama.database.Entity(pb_link.from_entity.uuid)
        to_entity = kama.database.Entity(pb_link.to_entity.uuid)
        from_entity.delete_link(to_entity, context=request_context)
        return Empty()

    def AddPermission(self, pb_permission, context):
        request_context = kama.context.get_request_context(context)
        role = kama.database.Entity(pb_permission.role.uuid)
        entity = kama.database.Entity(pb_permission.entity.uuid)
        permission = entity.add_permission(role, pb_permission.name, context=request_context)
        pb_permission.uuid = permission.uuid
        return pb_permission

    def DeletePermission(self, pb_permission, context):
        request_context = kama.context.get_request_context(context)
        role = kama.database.Entity(pb_permission.role.uuid)
        entity = kama.database.Entity(pb_permission.entity.uuid)
        entity.delete_permission(role, pb_permission.name, context=request_context)
        return Empty()

def main(args):
    if args.init_schema:
        kama.database.schema_init()
        return

    server_cert = open(os.path.join('secrets', 'server.cert'), 'r').read()
    server_key = open(os.path.join('secrets', 'server.key'), 'r').read()
    root_cert = open(os.path.join('secrets', 'ca-cert.pem'), 'r').read()
    creds = grpc.ssl_server_credentials([(server_key, server_cert)], root_cert, require_client_auth=True)

    servicer = DatabaseServicer()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=100))
    kama_pb2_grpc.add_KamaDatabaseServicer_to_server(servicer, server)
    server.add_secure_port('[::]:8444', creds)
    server.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        server.stop(0)


def setup_arguments(parser):
    parser.add_argument('--init-schema', action='store_true')
    parser.set_defaults(func=main)


def cli():
    parser = argparse.ArgumentParser()
    setup_arguments(parser)
    args = parser.parse_args()
    main(args)
