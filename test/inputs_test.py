from datetime import datetime
from flask import json
import pika
import pytest

from cacahuate.models import Execution

from .utils import make_auth, make_pointer, make_user


def test_all_inputs(client, config, mongo):
    user = make_user('juan', 'Juan')

    objeto = [
        {
            'ref': 'auth-form',
            'data': {
                'name': 'Algo',
                'datetime': datetime.now().isoformat()+'Z',
                'secret': '123456',
                'gender': 'female',
                'interests': ['science', 'music'],
                'elections': 'amlo',
            },
        },
    ]

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(user)}, data=json.dumps({
        'process_name': 'all-inputs',
        'form_array': objeto
    }))

    assert res.status_code == 201


def test_datetime_error(client, mocker, config):
    objeto = [
        {
            'ref': 'auth-form',
            'data': {
                'name': 'Algo',
                'datetime': 'FECHA ERRONEA',
                'secret': '123456',
                'interests': ['science', 'music'],
                'gender': 'male',
                'elections': 'amlo',
            },
        },
    ]
    juan = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'all-inputs',

        'form_array': objeto
    }))

    assert res.status_code == 400


def test_visible_document_provider(client, mocker, config):
    res = client.get('/v1/process')

    body = json.loads(res.data)
    document_process = list(
        filter(
            lambda xml: xml['id'] == 'document', body['data']
        )
    )[0]

    assert res.status_code == 200
    assert document_process['form_array'][0] == {
        'ref': 'doc-form',
        'inputs': [
            {
                'label': 'Documento de identidad oficial',
                'name': 'identity_card',
                'provider': 'doqer',
                'required': True,
                'type': 'file',
            },
        ],
    }


def test_allow_document(client, mocker, config):
    form_array = [
        {
            'ref': 'doc-form',
            'data': {
                'identity_card': {
                    'id': 102214720680704176,
                    'mime': 'image/gif',
                    'name': 'credencial de elector',
                    'type': 'doqer:file',
                },
            },
        },
    ]
    juan = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'document',
        'form_array': form_array,
    }))

    assert res.status_code == 201


def test_deny_invalid_document(client, mocker, config):
    form_array = [
        {
            'ref': 'doc-form',
            'data': {
                'identity_card': {
                    'this': 'is invalid'
                },
            },
        },
    ]
    juan = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'document',
        'form_array': form_array,
    }))

    assert res.status_code == 400

    form_array = [
        {
            'ref': 'doc-form',
            'data': {
                'identity_card': 'also invalid'
            },
        },
    ]

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'document',
        'form_array': form_array,
    }))

    assert res.status_code == 400


def test_check_errors(client, mocker, config):
    objeto = [
        {
            'ref': 'auth-form',
            'data': {
                'name': 'Algo',
                'datetime': datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
                'secret': '123456',
                'interests': 12,
                'gender': 'male',
                'elections': 'amlo',
            },
        },
    ]
    juan = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'all-inputs',
        'form_array': objeto
    }))

    assert res.status_code == 400

    objeto = [
        {
            'ref': 'auth-form',
            'data': {
                'name': 'Algo',
                'datetime': datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
                'secret': '123456',
                'interests': ["science", "wrong"],
                'gender': 'male',
                'elections': 'amlo',
            },
        },
    ]

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'all-inputs',
        'form_array': objeto
    }))

    assert res.status_code == 400


def test_radio_errors(client, mocker, config):
    objeto = [
        {
            'ref': 'auth-form',
            'data': {
                'name': 'Algo',
                'datetime': datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
                'secret': '123456',
                'interests': ["science"],
                'gender': [],
                'elections': 'amlo',
            },
        },
    ]
    juan = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'all-inputs',
        'form_array': objeto
    }))

    assert res.status_code == 400

    objeto = [
        {
            'ref': 'auth-form',
            'data': {
                'name': 'Algo',
                'datetime': datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
                'secret': '123456',
                'interests': ["science", "wrong"],
                'gender': 'error',
                'elections': 'amlo',
            },
        },
    ]

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'all-inputs',
        'form_array': objeto
    }))

    assert res.status_code == 400


def test_select_errors(client, mocker, config):
    objeto = [
        {
            'ref': 'auth-form',
            'data': {
                'name': 'Algo',
                'datetime': datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
                'secret': '123456',
                'interests': ["science"],
                'gender': "male",
                'elections': [],
            },
        },
    ]
    juan = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'all-inputs',
        'form_array': objeto
    }))

    assert res.status_code == 400

    objeto = [
        {
            'ref': 'auth-form',
            'data': {
                'name': 'Algo',
                'datetime': datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
                'secret': '123456',
                'interests': ["science", "wrong"],
                'gender': "male",
                'elections': "error",
            },
        },
    ]

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'all-inputs',
        'form_array': objeto
    }))

    assert res.status_code == 400


def test_validate_form_multiple(client):
    juan = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'form-multiple',
        'form_array': [
            {
                'ref': 'single-form',
                'data': {
                    'name': 'jorge',
                },
            },
            {
                'ref': 'multiple-form',
                'data': {},
            },
        ],
    }))

    assert res.status_code == 400
    assert json.loads(res.data) == {
        'errors': [
            {
                'detail': '\'phone\' is required',
                'where': 'request.body.form_array.1.phone',
                'code': 'validation.required',
            },
        ]
    }


def test_validate_form_multiple_error_position(client):
    juan = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'form-multiple',
        'form_array': [
            {
                'ref': 'single-form',
                'data': {
                    'name': 'jorge',
                },
            },
            {
                'ref': 'multiple-form',
                'data': {
                    'phone': '12432',
                },
            },
            {
                'ref': 'multiple-form',
                'data': {},
            },
        ],
    }))

    assert res.status_code == 400
    assert json.loads(res.data) == {
        'errors': [
            {
                'code': 'validation.required',
                'detail': '\'phone\' is required',
                'where': 'request.body.form_array.2.phone',
            },
        ]
    }


@pytest.mark.skip
def test_can_send_no_form(client):
    ''' assert that a form that passes valudation does not ask for information
    in terms of the form count '''
    assert False


@pytest.mark.skip
def test_default_inputs(client):
    ''' do not send any value. Values set must be defaults '''
    user = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(user)}, data=json.dumps({
        'process_name': 'all-default-input',
        'form_array': [
            {
                'ref': 'auth-form',
                'data': {}
            },
        ],
    }))

    ques = Questionaire.get_all()[0].to_json()

    assert res.status_code == 201

    # text
    assert ques['data']['name'] == 'Jon Snow'
    # datetime
    assert_near_date(datetime.strptime(
        ques['data']['datetime'],
        "%Y-%m-%dT%H:%M:%S.%fZ"
    ))
    # password
    assert ques['data']['secret'] == 'dasdasd'
    # checkbox
    assert False
    # radio
    assert False
    # select
    assert False
    # file
    assert False


@pytest.mark.skip
def test_required_inputs_with_defaults(client):
    ''' all inputs are required but all of them have defaults '''
    user = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(user)}, data=json.dumps({
        'process_name': 'not-default-required-input',
        'form_array': [
            {
                'ref': 'auth-form',
                'data': {}
            },
        ],
    }))

    ques = Questionaire.get_all()[0].to_json()

    assert res.status_code == 201

    # text
    assert ques['data']['name'] == 'Jon Snow'
    # datetime
    assert_near_date(datetime.strptime(
        ques['data']['datetime'],
        "%Y-%m-%dT%H:%M:%S.%fZ"
    ))
    # password
    assert ques['data']['secret'] == 'dasdasd'
    # checkbox
    assert False
    # radio
    assert False
    # select
    assert False
    # file
    assert False


def test_start_with_correct_form_order(client, mocker, mongo, config):
    user = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(user)}, data=json.dumps({
        'process_name': 'form-multiple',
        'form_array': [
            {
                'ref': 'single-form',
                'data': {
                    'name': 'og',
                },
            },
            {
                'ref': 'multiple-form',
                'data': {
                    'phone': '3312345678'
                },
            },
            {
                'ref': 'multiple-form',
                'data': {
                    'phone': '3312345678'
                },
            },
            {
                'ref': 'multiple-form',
                'data': {
                    'phone': '3312345678'
                },
            },
        ],
    }))

    assert res.status_code == 201


def test_start_with_incorrect_form_order(client, mocker, mongo, config):
    user = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(user)}, data=json.dumps({
        'process_name': 'form-multiple',
        'form_array': [
            {
                'ref': 'multiple-form',
                'data': {
                    'phone': '3312345678'
                },
            },
            {
                'ref': 'multiple-form',
                'data': {
                    'phone': '3312345678'
                },
            },
            {
                'ref': 'multiple-form',
                'data': {
                    'phone': '3312345678'
                },
            },
            {
                'ref': 'single-form',
                'data': {
                    'name': 'og',
                },
            },
        ],
    }))

    assert res.status_code == 400


def test_hidden_input(client, mocker, config, mongo):
    mocker.patch(
        'pika.adapters.blocking_connection.'
        'BlockingChannel.basic_publish'
    )

    juan = make_user('juan', 'Juan')

    res = client.post('/v1/execution', headers={**{
        'Content-Type': 'application/json',
    }, **make_auth(juan)}, data=json.dumps({
        'process_name': 'input-hidden',
        'form_array': [{
            'ref': 'start_form',
            'data': {
                'data': 'yes',
            },
        }],
    }))

    assert res.status_code == 201

    exc = Execution.get_all()[0]
    ptr = exc.proxy.pointers.get()[0]

    pika.adapters.blocking_connection.BlockingChannel.\
        basic_publish.assert_called_once()

    args = pika.adapters.blocking_connection.\
        BlockingChannel.basic_publish.call_args[1]

    json_message = {
        'command': 'step',
        'pointer_id': ptr.id,
        'user_identifier': 'juan',
        'input': [{
            '_type': 'form',
            'ref': 'start_form',
            'state': 'valid',
            'inputs': {
                '_type': ':sorted_map',
                'items': {
                    'data': {
                        'label': 'data',
                        'type': 'text',
                        'value': 'yes',
                        'value_caption': 'yes',
                        'name': 'data',
                        'state': 'valid',
                        'hidden': True,
                    },
                },
                'item_order': ['data'],
            },
        }],
    }

    assert json.loads(args['body']) == json_message
