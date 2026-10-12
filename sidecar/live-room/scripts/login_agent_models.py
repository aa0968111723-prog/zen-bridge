"""Official HF device login restricted to already-approved public gated models."""
import json
import os


def main():
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    from huggingface_hub import constants,get_token
    from huggingface_hub.utils._http import get_session,hf_raise_for_status
    from huggingface_hub.utils._oauth_device import poll_device_token
    from huggingface_hub._login import _save_oauth_token
    if get_token():
        print('Hugging Face local login is already configured.');return
    if constants.ENDPOINT!='https://huggingface.co':
        raise ValueError('Authentication requires the official Hugging Face endpoint')
    reply=get_session().post(constants.ENDPOINT+'/oauth/device',
        data={'client_id':constants.DEVICE_CODE_OAUTH_CLIENT_ID,'scope':'openid gated-repos'},timeout=15)
    hf_raise_for_status(reply)
    device=reply.json()
    device.setdefault('interval',5);device.setdefault('expires_in',300)
    device.setdefault('verification_uri_complete',device['verification_uri'])
    print(json.dumps({'verification_uri':device['verification_uri_complete'],
                      'user_code':device['user_code'],'permission':'read approved public gated models'}),flush=True)
    response=poll_device_token(device)
    scopes=response.get('scope')
    if scopes is not None and not set(scopes.split())<={'openid','gated-repos'}:
        raise RuntimeError('Permission exceeded model-download access; login was not saved')
    _save_oauth_token(response)
    print('HF_LOCAL_LOGIN_SAVED',flush=True)


if __name__=='__main__':
    main()
