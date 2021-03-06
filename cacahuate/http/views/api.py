from coralillo.errors import ModelNotFoundError
from datetime import datetime
from flask import g
from flask import request, jsonify, json
import os
import pika
import pymongo
import re

from cacahuate.errors import InputError, RequiredListError, RequiredDictError
from cacahuate.errors import RequiredInputError, RequiredStrError
from cacahuate.errors import InvalidInputError
from cacahuate.errors import ProcessNotFound, ElementNotFound, MalformedProcess
from cacahuate.http.errors import BadRequest, NotFound, UnprocessableEntity
from cacahuate.http.errors import Forbidden
from cacahuate.http.middleware import requires_json, requires_auth, pagination
from cacahuate.http.validation import validate_json, validate_auth
from cacahuate.http.wsgi import app, mongo
from cacahuate.models import Execution, Pointer, User
from cacahuate.node import make_node
from cacahuate.rabbit import get_channel
from cacahuate.xml import Xml, form_to_dict, get_text, get_element_by
from cacahuate.node import make_input


DATE_FIELDS = [
    'started_at',
    'finished_at',
]


def json_prepare(obj):
    if obj.get('_id'):
        del obj['_id']

    for field in DATE_FIELDS:
        if obj.get(field) and type(obj[field]) == datetime:
            obj[field] = obj[field].isoformat()

    return obj


@app.route('/', methods=['GET', 'POST'])
@requires_json
def index():
    if request.method == 'GET':
        return {
            'hello': 'world',
        }
    elif request.method == 'POST':
        return request.json


@app.route('/v1/execution', methods=['GET'])
@pagination
def execution_list():
    collection = mongo.db[app.config['EXECUTION_COLLECTION']]

    dict_args = request.args.to_dict()

    query = dict(
        (k, dict_args[k]) for k in dict_args
        if k not in app.config['INVALID_FILTERS']
    )

    # filter for user_identifier
    user_identifier = query.pop('user_identifier', None)
    if user_identifier is not None:
        user = User.get_by('identifier', user_identifier)
        if user is not None:
            execution_list = [item.id for item in user.proxy.activities.get()]
        else:
            execution_list = []
        query['id'] = {
            '$in': execution_list,
        }

    return jsonify({
        "data": list(map(
            json_prepare,
            collection.find(query).sort([
                ('started_at', pymongo.DESCENDING)
            ]).skip(g.offset).limit(g.limit)
        )),
    })


@app.route('/v1/execution/<id>', methods=['GET'])
def process_status(id):
    collection = mongo.db[app.config['EXECUTION_COLLECTION']]

    try:
        exc = next(collection.find({'id': id}))
    except StopIteration:
        raise ModelNotFoundError(
            'Specified execution never existed, and never will'
        )

    return jsonify({
        'data': json_prepare(exc),
    })


@app.route('/v1/execution/<id>', methods=['PATCH'])
def execution_patch(id):
    execution = Execution.get_or_exception(id)
    collection = mongo.db[app.config['EXECUTION_COLLECTION']]
    execution_state = next(collection.find({'id': id}))

    validate_json(request.json, ['comment', 'inputs'])

    xml = Xml.load(app.config, execution.process_name, direct=True)
    dom = xml.get_dom()

    if type(request.json['inputs']) != list:
        raise RequiredListError('inputs', 'request.body.inputs')

    processed_inputs = []

    for i, field in enumerate(request.json['inputs']):
        if type(field) != dict:
            raise RequiredDictError(str(i), 'request.body.inputs.{}'.format(i))

        if 'ref' not in field:
            raise RequiredInputError('id',
                                     'request.body.inputs.{}.ref'.format(i))

        if type(field['ref']) != str:
            raise RequiredStrError('ref',
                                   'request.body.inputs.{}.ref'.format(i))

        # check down the state tree for existence of the requested ref
        processed_ref = []
        pieces = field['ref'].split('.')

        try:
            node_id = pieces.pop(0)
            node_state = execution_state['state']['items'][node_id]
        except IndexError:
            raise InputError(
                'Missing segment in ref for node_id',
                'request.body.inputs.{}.ref'.format(i),
                'validation.invalid')
        except KeyError:
            raise InputError(
                'node {} not found'.format(node_id),
                'request.body.inputs.{}.ref'.format(i),
                'validation.invalid')

        if node_state['type'] != 'action':
            raise InputError(
                'only action nodes may be patched',
                'request.body.inputs.{}.ref'.format(i),
                'validation.invalid')

        processed_ref.append(node_id)

        # node xml element
        node = get_element_by(dom, 'action', 'id', node_id)

        if len(node_state['actors']['items']) == 1:
            only_key = list(node_state['actors']['items'].keys())[0]
            actor_state = node_state['actors']['items'][only_key]
        else:
            try:
                actor_username = pieces.pop(0)
                actor_state = node_state['actors']['items'][actor_username]
            except IndexError:
                raise InputError(
                    'Missing segment in ref for actor username',
                    'request.body.inputs.{}.ref'.format(i),
                    'validation.invalid')
            except KeyError:
                raise InputError(
                    'actor {} not found'.format(actor_username),
                    'request.body.inputs.{}.ref'.format(i),
                    'validation.invalid')

        processed_ref.append(actor_state['user']['identifier'])

        try:
            form_ref = pieces.pop(0)
        except IndexError:
            raise InputError(
                'Missing segment in ref for form ref',
                'request.body.inputs.{}.ref'.format(i),
                'validation.invalid')

        if re.match(r'\d+', form_ref):
            try:
                form_index = int(form_ref)
                form_state = actor_state['forms'][form_index]
            except KeyError:
                raise InputError(
                    'form index {} not found'.format(form_ref),
                    'request.body.inputs.{}.ref'.format(i),
                    'validation.invalid')
        else:
            matching_forms = list(map(
                lambda f: f['ref'] == form_ref,
                actor_state['forms']
            ))
            form_count = len(list(filter(lambda x: x, matching_forms)))

            if form_count == 1:
                form_index = matching_forms.index(True)
                form_state = actor_state['forms'][form_index]
            elif form_count == 0:
                raise InputError(
                    'No forms with ref {} in node'.format(form_ref),
                    'request.body.inputs.{}.ref'.format(i),
                    'validation.invalid'
                )
            else:
                raise InputError(
                    'More than one form with ref {}'.format(form_ref),
                    'request.body.inputs.{}.ref'.format(i),
                    'validation.invalid'
                )

        processed_ref.append(str(form_index) + ':' + form_state['ref'])

        # form xml element
        form = get_element_by(node, 'form', 'id', form_state['ref'])

        try:
            input_name = pieces.pop(0)
            form_state['inputs']['items'][input_name]
        except IndexError:
            raise InputError(
                'Missing segment in ref for input name',
                'request.body.inputs.{}.ref'.format(i),
                'validation.invalid')
        except KeyError:
            raise InputError(
                'input {} not found'.format(input_name),
                'request.body.inputs.{}.ref'.format(i),
                'validation.invalid'
            )

        processed_ref.append(input_name)

        processed_inputs.append({
            'ref': '.'.join(processed_ref),
        })

        # input xml element
        input_el = get_element_by(form, 'input', 'name', input_name)

        if 'value' in field:
            try:
                input_obj = make_input(input_el)
                value = input_obj.validate(field['value'], 0)
                caption = input_obj.make_caption(value)

                processed_inputs[-1]['value'] = value
                processed_inputs[-1]['value_caption'] = caption
            except InputError as e:
                raise InputError(
                    'value invalid: {}'.format(str(e)),
                    'request.body.inputs.{}.value'.format(i),
                    'validation.invalid')

    channel = get_channel()
    channel.basic_publish(
        exchange='',
        routing_key=app.config['RABBIT_QUEUE'],
        body=json.dumps({
            'command': 'patch',
            'execution_id': execution.id,
            'comment': request.json['comment'],
            'inputs': processed_inputs,
        }),
        properties=pika.BasicProperties(
            delivery_mode=2,
        ),
    )

    return jsonify({
        'data': 'accepted',
    }), 202


@app.route('/v1/execution/<id>/user', methods=['PUT'])
@requires_auth
def execution_add_user(id):
    ''' adds the user as a candidate for solving the given node, only if the
    node has an active pointer. '''
    # TODO possible race condition introduced here. How does this code work in
    # case the handler is moving the pointer?

    # get execution
    execution = Execution.get_or_exception(id)

    # validate the members needed
    validate_json(request.json, ['identifier', 'node_id'])

    identifier = request.json['identifier']
    node_id = request.json['node_id']

    # get actual pointer
    try:
        pointer = next(execution.proxy.pointers.q().filter(node_id=node_id))
    except StopIteration:
        raise BadRequest([{
            'detail': f'{node_id} does not have a live pointer',
            'code': 'validation.no_live_pointer',
            'where': 'request.body.node_id',
        }])

    # get user
    user = User.get_by('identifier', identifier)
    if user is None:
        raise InvalidInputError('user_id', 'request.body.identifier')

    # update user
    user.proxy.tasks.add(pointer)

    # update pointer
    collection = mongo.db[app.config['POINTER_COLLECTION']]
    db_pointer = collection.find_one({'id': pointer.id})
    user_json = user.to_json()
    notified_users = db_pointer.get('notified_users', [])

    if user_json not in notified_users:
        notified_users.append(user.to_json())

    collection.update_one(
        {'id': pointer.id},
        {'$set': {'notified_users': notified_users}},
    )

    return jsonify(user_json), 200


@app.route('/v1/execution/<id>', methods=['DELETE'])
@requires_auth
def delete_process(id):
    execution = Execution.get_or_exception(id)

    channel = get_channel()
    channel.basic_publish(
        exchange='',
        routing_key=app.config['RABBIT_QUEUE'],
        body=json.dumps({
            'command': 'cancel',
            'execution_id': execution.id,
        }),
        properties=pika.BasicProperties(
            delivery_mode=2,
        ),
    )

    return jsonify({
        'data': 'accepted',
    }), 202


@app.route('/v1/execution', methods=['POST'])
@requires_auth
@requires_json
def start_process():
    validate_json(request.json, ['process_name'])

    try:
        xml = Xml.load(app.config, request.json['process_name'])
    except ProcessNotFound:
        raise NotFound([{
            'detail': '{} process does not exist'
                      .format(request.json['process_name']),
            'where': 'request.body.process_name',
        }])
    except MalformedProcess as e:
        raise UnprocessableEntity([{
            'detail': str(e),
            'where': 'request.body.process_name',
        }])

    xmliter = iter(xml)
    node = make_node(next(xmliter), xmliter)

    # Check for authorization
    validate_auth(node, g.user)

    # check if there are any forms present
    input = node.validate_input(request.json)

    # get rabbit channel for process queue
    channel = get_channel()

    execution = xml.start(node, input, mongo.db, channel, g.user.identifier)

    return {
        'data': execution.to_json(),
    }, 201


@app.route('/v1/pointer', methods=['POST'])
@requires_auth
@requires_json
def continue_process():
    validate_json(request.json, ['execution_id', 'node_id'])

    execution_id = request.json['execution_id']
    node_id = request.json['node_id']

    try:
        execution = Execution.get_or_exception(execution_id)
    except ModelNotFoundError:
        raise BadRequest([{
            'detail': 'execution_id is not valid',
            'code': 'validation.invalid',
            'where': 'request.body.execution_id',
        }])

    xml = Xml.load(app.config, execution.process_name, direct=True)
    xmliter = iter(xml)

    try:
        continue_point = make_node(
            xmliter.find(lambda e: e.getAttribute('id') == node_id),
            xmliter
        )
    except ElementNotFound:
        raise BadRequest([{
            'detail': 'node_id is not a valid node',
            'code': 'validation.invalid_node',
            'where': 'request.body.node_id',
        }])

    try:
        pointer = next(execution.proxy.pointers.q().filter(node_id=node_id))
    except StopIteration:
        raise BadRequest([{
            'detail': 'node_id does not have a live pointer',
            'code': 'validation.no_live_pointer',
            'where': 'request.body.node_id',
        }])

    # Check for authorization
    if pointer not in g.user.proxy.tasks:
        raise Forbidden([{
            'detail': 'Provided user does not have this task assigned',
            'where': 'request.authorization',
        }])

    # Validate asociated forms
    collected_input = continue_point.validate_input(request.json)

    # trigger rabbit
    channel = get_channel()
    channel.basic_publish(
        exchange='',
        routing_key=app.config['RABBIT_QUEUE'],
        body=json.dumps({
            'command': 'step',
            'pointer_id': pointer.id,
            'user_identifier': g.user.identifier,
            'input': collected_input,
        }),
        properties=pika.BasicProperties(
            delivery_mode=2,
        ),
    )

    return {
        'data': 'accepted',
    }, 202


@app.route('/v1/process', methods=['GET'])
def list_process():
    def add_form(xml):
        json_xml = xml.to_json()
        forms = []
        xmliter = iter(xml)
        first_node = next(xmliter)
        xmliter.parser.expandNode(first_node)

        for form in first_node.getElementsByTagName('form'):
            forms.append(form_to_dict(form))

        json_xml['form_array'] = forms

        return json_xml

    return jsonify({
        'data': list(filter(
            lambda x: x,
            map(
                add_form,
                Xml.list(app.config),
            )
        )),
    })


@app.route('/v1/process/<name>', methods=['GET'])
def find_process(name):
    version = request.args.get('version', '')

    if version:
        version = ".{}".format(version)

    process_name = "{}{}".format(name, version)

    try:
        xml = Xml.load(app.config, process_name)
    except ProcessNotFound:
        raise NotFound([{
            'detail': '{} process does not exist'
                      .format(process_name),
            'where': 'request.body.process_name',
        }])

    return jsonify({
        'data': xml.to_json()
    })


@app.route('/v1/process/<name>.xml', methods=['GET'])
def xml_process(name):
    version = request.args.get('version', '')

    if version:
        version = ".{}".format(version)

    process_name = "{}{}".format(name, version)

    try:
        xml = Xml.load(app.config, process_name)
    except ProcessNotFound:
        raise NotFound([{
            'detail': '{} process does not exist'
                      .format(process_name),
            'where': 'request.body.process_name',
        }])
    ruta = os.path.join(app.config['XML_PATH'], xml.filename)
    return open(ruta).read(), {'Content-Type': 'text/xml; charset=utf-8'}


@app.route('/v1/activity', methods=['GET'])
@requires_auth
def list_activities():
    activities = g.user.proxy.activities.get()

    return jsonify({
        'data': list(map(
            lambda a: a.to_json(include=['*', 'execution']),
            activities
        )),
    })


@app.route('/v1/task', methods=['GET'])
@requires_auth
def task_list():
    tasks = g.user.proxy.tasks.get()

    return jsonify({
        'data': list(map(
            lambda t: t.to_json(include=['*', 'execution']),
            tasks
        )),
    })


@app.route('/v1/task/<id>', methods=['GET'])
@requires_auth
def task_read(id):
    pointer = Pointer.get_or_exception(id)

    if pointer not in g.user.proxy.tasks:
        raise Forbidden([{
            'detail': 'Provided user does not have this task assigned',
            'where': 'request.authorization',
        }])

    execution = pointer.proxy.execution.get()
    collection = mongo.db[app.config['EXECUTION_COLLECTION']]
    state = collection.find_one({
        'id': execution.id,
    })

    xml = Xml.load(
        app.config,
        execution.process_name,
        direct=True
    )
    xmliter = iter(xml)
    node = xmliter.find(lambda e: e.getAttribute('id') == pointer.node_id)

    xmliter.parser.expandNode(node)

    # Response body
    json_data = pointer.to_json(include=['*', 'execution'])

    # Append node info
    json_data['node_type'] = node.tagName

    # Append forms
    forms = []
    for form in node.getElementsByTagName('form'):
        forms.append(form_to_dict(form))
    json_data['form_array'] = forms

    # If any append previous work done
    node_state = state['state']['items'][pointer.node_id]
    node_actors = node_state['actors']

    user_identifier = g.user.identifier
    if user_identifier in node_actors['items']:
        action = node_actors['items'][user_identifier]

        json_data['prev_work'] = action['forms']

    # Append validation
    if node.tagName == 'validation':
        deps = list(map(
            lambda node: get_text(node),
            node.getElementsByTagName('dep')
        ))

        fields = []
        for dep in deps:
            form_ref, input_name = dep.split('.')

            # TODO this could be done in O(log N + K)
            for node in state['state']['items'].values():
                if node['state'] != 'valid':
                    continue

                for identifier in node['actors']['items']:
                    actor = node['actors']['items'][identifier]
                    if actor['state'] != 'valid':
                        continue

                    for form_ix, form in enumerate(actor['forms']):
                        if form['state'] != 'valid':
                            continue

                        if form['ref'] != form_ref:
                            continue

                        if input_name not in form['inputs']['items']:
                            continue

                        input = form['inputs']['items'][input_name]

                        state_ref = [
                            node['id'],
                            identifier,
                            str(form_ix),
                        ]
                        state_ref = '.'.join(state_ref)
                        state_ref = state_ref + ':' + dep

                        field = {
                            'ref': state_ref,
                            **input,
                        }
                        del field['state']

                        fields.append(field)

        json_data['fields'] = fields

    return jsonify({
        'data': json_data,
    })


@app.route('/v1/inbox', methods=['GET'])
@pagination
def data_mix():
    dict_args = request.args.to_dict()

    # get queries

    # execution's query
    exe_query = dict(
        (k, dict_args[k]) for k in dict_args
        if k not in app.config['INVALID_FILTERS']
    )

    # get pointer's query
    ptr_query = {}
    for item in exe_query.copy():
        if item.startswith('pointer.'):
            group, value = item.split('.', 1)
            ptr_query[value] = exe_query.pop(item)

    # filter for exclude/include
    exclude_fields = exe_query.pop('exclude', '')
    exclude_list = [s.strip() for s in exclude_fields.split(',') if s]
    exclude_map = {item: 0 for item in exclude_list}

    include_fields = exe_query.pop('include', '')
    include_list = [s.strip() for s in include_fields.split(',') if s]
    include_map = {item: 1 for item in include_list}

    prjct = {**include_map} or {**exclude_map}

    # filter for user_identifier
    user_identifier = exe_query.pop('user_identifier', None)
    if user_identifier is not None:
        user = User.get_by('identifier', user_identifier)
        if user is not None:
            execution_list = [item.id for item in user.proxy.activities.get()]

            for item in user.proxy.tasks.get():
                execution_list.append(item.execution)
        else:
            execution_list = []

        exe_query['id'] = {
            '$in': execution_list,
        }

    # pipeline
    # all special cases should be handled before this

    # execution's case
    exe_pipeline = [
        {'$match': exe_query},
    ]

    exe_collection = mongo.db[app.config['EXECUTION_COLLECTION']]
    exe_cursor = exe_collection.aggregate(exe_pipeline)

    exe_ids = list(map(
        lambda item: item['id'],
        exe_cursor,
    ))

    # pointer's case
    ptr_pipeline = [
        {'$match': ptr_query},
        {'$group': {
            '_id': None,
            'executions': {'$push': '$execution.id'},
        }},
    ]

    ptr_collection = mongo.db[app.config['POINTER_COLLECTION']]
    ptr_cursor = ptr_collection.aggregate(ptr_pipeline)

    ptr_ids = []
    for item in ptr_cursor:
        ptr_ids += item['executions']

    # mix both lists

    def intersection(lst1, lst2):
        temp = set(lst2)
        lst3 = [value for value in lst1 if value in temp]
        return lst3

    if exe_ids and ptr_ids:
        execution_ids = intersection(exe_ids, ptr_ids)
    else:
        execution_ids = exe_ids or ptr_ids

    # build results
    ptr_pipeline = [
        {'$match': {'execution.id': {'$in': execution_ids}}},
        {'$sort': {'started_at': -1}},
        {'$group': {
            '_id': '$execution.id',
            'latest': {'$first': '$$ROOT'},
        }},
        {'$replaceRoot': {'newRoot': '$latest'}},
        # TODO: DO NOT CREATE COLLECTION
        {'$out': 'ptr_aux_collection'},
    ]

    ptr_collection.aggregate(ptr_pipeline)

    exe_pipeline = [
        {'$match': {'id': {'$in': execution_ids}}},
        # TODO: FIND ANOTHER WAY TO ADD POINTER
        {'$lookup': {
            'from': 'ptr_aux_collection',
            'localField': 'id',
            'foreignField': 'execution.id',
            'as': 'pointer',
        }},
        {'$sort': {'started_at': -1}},
        {'$skip': g.offset},
        {'$limit': g.limit},
    ]

    if prjct:
        # TODO: THE ABOVE LOOKUP IS REQUIRED TO USE include/exclude=pointer.foo
        exe_pipeline.append({'$project': prjct})

    def data_mix_json_prepare(obj):
        if obj.get('pointer') is not None:
            try:
                obj['pointer'] = obj['pointer'][0]
                obj['pointer'].pop('execution', {})
                obj['pointer'] = json_prepare(obj['pointer'])
            except IndexError:
                obj['pointer'] = None
        return json_prepare(obj)

    return jsonify({
        'data': list(map(
            data_mix_json_prepare,
            exe_collection.aggregate(exe_pipeline),
        ))
    })


@app.route('/v1/log', methods=['GET'])
@pagination
def all_logs():
    collection = mongo.db[app.config['POINTER_COLLECTION']]

    dict_args = request.args.to_dict()

    query = dict(
        (k, dict_args[k]) for k in dict_args
        if k not in app.config['INVALID_FILTERS']
    )

    # filter for user_identifier
    user_identifier = query.pop('user_identifier', None)
    if user_identifier is not None:
        user = User.get_by('identifier', user_identifier)
        if user is not None:
            pointer_list = [item.id for item in user.proxy.tasks.get()]
        else:
            pointer_list = []
        query['id'] = {
            '$in': pointer_list,
        }

    pipeline = [
        {'$match': query},
        {'$sort': {'started_at': -1}},
        {'$group': {
            '_id': '$execution.id',
            'latest': {'$first': '$$ROOT'},
        }},
        {'$replaceRoot': {'newRoot': '$latest'}},
        {'$sort': {'started_at': -1}},
        {'$skip': g.offset},
        {'$limit': g.limit},
    ]

    return jsonify({
        'data': list(map(
            json_prepare,
            collection.aggregate(pipeline),
        )),
    })


@app.route('/v1/log/<id>', methods=['GET'])
@pagination
def list_logs(id):
    collection = mongo.db[app.config['POINTER_COLLECTION']]
    node_id = request.args.get('node_id')
    query = {'execution.id': id}

    if node_id:
        query['node.id'] = node_id

    return jsonify({
        "data": list(map(
            json_prepare,
            collection.find(query).skip(g.offset).limit(g.limit).sort([
                ('started_at', pymongo.DESCENDING)
            ])
        )),
    })


@app.route('/v1/process/<id>/statistics', methods=['GET'])
def node_statistics(id):
    collection = mongo.db[app.config['POINTER_COLLECTION']]
    query = [
        {"$match": {"process_id": id}},
        {"$project": {
            "process_id": "$process_id",
            "node": "$node.id",
            "difference_time": {
                "$subtract": ["$finished_at", "$started_at"],
            },
        }},
        {"$group": {
            "_id": {"process_id": "$process_id", "node": "$node"},
            "process_id": {"$first": "$process_id"},
            "node": {"$first": "$node"},
            "max": {
                "$max": {
                    "$divide": ["$difference_time", 1000],
                },
            },
            "min": {
                "$min": {
                    "$divide": ["$difference_time", 1000],
                },
            },
            "average": {
                "$avg": {
                    "$divide": ["$difference_time", 1000],
                },
            },
        }},

        {"$sort": {"execution": 1, "node": 1}}
    ]
    return jsonify({
        "data": list(map(
            json_prepare,
            collection.aggregate(query)
        )),
    })


@app.route('/v1/process/statistics', methods=['GET'])
@pagination
def process_statistics():
    collection = mongo.db[app.config['EXECUTION_COLLECTION']]
    query = [
        {"$match": {"status": "finished"}},
        {"$skip": g.offset},
        {"$limit": g.limit},
        {"$project": {"difference_time": {
            "$subtract": ["$finished_at", "$started_at"]
            }, "process":{"id": "$process.id"},
        }},

        {"$group": {
            "_id": "$process.id",
            "process": {"$first": "$process.id"},
            "max": {
                "$max": {
                    "$divide": ["$difference_time", 1000],
                },
            },
            "min": {
                "$min": {
                    "$divide": ["$difference_time", 1000],
                },
            },
            "average": {
                "$avg": {
                    "$divide": ["$difference_time", 1000],
                },
            },

        }},
        {"$sort": {"process": 1}},
    ]

    return jsonify({
        "data": list(map(
            json_prepare,
            collection.aggregate(query)
        )),
    })
