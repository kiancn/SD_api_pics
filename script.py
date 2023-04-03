import base64
import io
import re
import time
from pathlib import Path

import gradio as gr
import modules.chat as chat
import modules.shared as shared
import requests
import torch
from PIL import Image
from datetime import date

torch._C._jit_set_profiling_mode(False)

# parameters which can be customized in settings.json of webui  
params = {
    'address': 'http://127.0.0.1:7860',
    'mode': 0, # modes of operation: 0 (Manual only), 1 (Immersive/Interactive - looks for words to trigger), 2 (Picturebook Adventure - Always on)
    'manage_VRAM': False,
    'save_img': False,
    'SD_model': 'NeverEndingDream', # not used right now
    'prompt_prefix': '(Masterpiece:1.1), detailed, intricate, colorful',
    'negative_prompt': '(worst quality, low quality:1.3)',
    'width': 512,
    'height': 512,
    'restore_faces': False,
    'seed': -1,
    'sampler_name': 'DDIM', 
    'steps': 32,
    'cfg_scale': 7
}

def update_params(): # somewhy the default extension params dict is not changed by settings.json, thus update it forcefully
    ext_name = "sd_api_pictures"
    for sett in shared.settings:
        if sett.startswith(f'{ext_name}-'):
            params.update({sett.replace(f'{ext_name}-',''): shared.settings[sett]})
update_params()

def give_VRAM_priority(actor):
    
    global shared, params

    if actor == 'SD':
        shared.gradio['unload_model_fn']()
        print("Requesting Auto1111 to re-load last checkpoint used…")
        response = requests.post(url=f'{params["address"]}/sdapi/v1/reload-checkpoint', json='')
        response.raise_for_status()

    elif actor == 'LLM':
        print("Requesting Auto1111 to vacate VRAM…")
        response = requests.post(url=f'{params["address"]}/sdapi/v1/unload-checkpoint', json='')
        response.raise_for_status()
        shared.gradio['reload_model_fn']()

    elif actor == 'set':
        print("VRAM mangement activated -- requesting Auto1111 to vacate VRAM…")
        response = requests.post(url=f'{params["address"]}/sdapi/v1/unload-checkpoint', json='')
        response.raise_for_status()

    elif actor == 'reset':
        print("VRAM mangement deactivated -- requesting Auto1111 to reload checkpoint")
        response = requests.post(url=f'{params["address"]}/sdapi/v1/reload-checkpoint', json='')
        response.raise_for_status()

    else:
        raise RuntimeError(f'Managing VRAM: "{actor}" is not a known state!')

    response.raise_for_status()
    del response

if params['manage_VRAM']: give_VRAM_priority('set')

samplers = ['DDIM', 'DPM++ 2M Karras'] # TODO: get the availible samplers with http://{address}}/sdapi/v1/samplers
SD_models = ['NeverEndingDream'] # TODO: get with http://{address}}/sdapi/v1/sd-models and allow user to select

streaming_state = shared.args.no_stream # remember if chat streaming was enabled
picture_response = False # specifies if the next model response should appear as a picture

def remove_surrounded_chars(string):
    # this expression matches to 'as few symbols as possible (0 upwards) between any asterisks' OR
    # 'as few symbols as possible (0 upwards) between an asterisk and the end of the string'
    return re.sub('\*[^\*]*?(\*|$)','',string)

def triggers_are_in(string):
    string = remove_surrounded_chars(string)
    # regex searches for send|main|message|me (at the end of the word) followed by
    # a whole word of image|pic|picture|photo|snap|snapshot|selfie|meme(s),
    # (?aims) are regex parser flags
    return bool(re.search('(?aims)(send|mail|message|me)\\b.+?\\b(image|pic(ture)?|photo|snap(shot)?|selfie|meme)s?\\b', string)) 

def input_modifier(string):
    """
    This function is applied to your text inputs before
    they are fed into the model.
    """
    global params
    if not params['mode'] == 1: # if not in immersive/interactive mode, do nothing
        return string

    if triggers_are_in(string): # if we're in it, check for trigger words
        toggle_generation(True)
        string = string.lower()
        if "of" in string:
            subject = string.split('of', 1)[1] # subdivide the string once by the first 'of' instance and get what's coming after it
            string = "Please provide a detailed and vivid description of " + subject
        else:
            string = "Please provide a detailed description of your appearance, your surroundings and what you are doing right now"

    return string

# Get and save the Stable Diffusion-generated picture
def get_SD_pictures(description):

    global params

    if params['manage_VRAM']: give_VRAM_priority('SD')

    payload = {
        "prompt": params['prompt_prefix'] + description,
        "seed": params['seed'],
        "sampler_name": params['sampler_name'],
        "steps": params['steps'],
        "cfg_scale": params['cfg_scale'],
        "width": params['width'],
        "height": params['height'],
        "restore_faces": params['restore_faces'],
        "negative_prompt": params['negative_prompt']
    }
    
    print(f'Prompting the image generator via the API on {params["address"]}...')
    response = requests.post(url=f'{params["address"]}/sdapi/v1/txt2img', json=payload)
    response.raise_for_status()
    r = response.json()

    visible_result = ""
    for img_str in r['images']:
        image = Image.open(io.BytesIO(base64.b64decode(img_str.split(",",1)[0])))
        if params['save_img']:
            variadic = f'{date.today().strftime("%Y_%m_%d")}/{shared.character}_{int(time.time())}'
            output_file = Path(f'extensions/sd_api_pictures/outputs/{variadic}.png')
            output_file.parent.mkdir(parents=True, exist_ok=True)
            image.save(output_file.as_posix())
            visible_result = visible_result + f'<img src="/file/extensions/sd_api_pictures/outputs/{variadic}.png" alt="{description}" style="max-width: unset; max-height: unset;">\n'
        else:
            # lower the resolution of received images for the chat, otherwise the log size gets out of control quickly with all the base64 values in visible history
            image.thumbnail((300, 300))
            buffered = io.BytesIO()
            image.save(buffered, format="JPEG")
            buffered.seek(0)
            image_bytes = buffered.getvalue()
            img_str = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()
            visible_result = visible_result + f'<img src="{img_str}" alt="{description}">\n'

    if params['manage_VRAM']: give_VRAM_priority('LLM')
    
    return visible_result

# TODO: how do I make the UI history ignore the resulting pictures (I don't want HTML to appear in history)
# and replace it with 'text' for the purposes of logging?
def output_modifier(string):
    """
    This function is applied to the model outputs.
    """
    global picture_response, params

    if not picture_response:
        return string

    string = remove_surrounded_chars(string)
    string = string.replace('"', '')
    string = string.replace('“', '')
    string = string.replace('\n', ' ')
    string = string.strip()

    if string == '':
        string = 'no viable description in reply, try regenerating'
        return string
    
    text = ""
    if (params['mode']<2):
        toggle_generation(False)
        text = f'*Sends a picture which portrays: “{string}”*'
    else:
        text = string

    string = get_SD_pictures(string) + "\n" + text

    return string 

def bot_prefix_modifier(string):
    """
    This function is only applied in chat mode. It modifies
    the prefix text for the Bot and can be used to bias its
    behavior.
    """

    return string

def toggle_generation(*args):
    global picture_response, shared, streaming_state
    if not args:
        picture_response = not picture_response
    else:
        picture_response = args[0]

    shared.args.no_stream = True if picture_response else streaming_state # Disable streaming cause otherwise the SD-generated picture would return as a dud
    shared.processing_message = "*Is sending a picture...*" if picture_response else "*Is typing...*"

def filter_address(address):
    address = address.strip()
    # address = re.sub('http(s)?:\/\/|\/$','',address) # remove starting http:// OR https:// OR trailing slash
    address = re.sub('\/$', '', address) # remove trailing /s
    if not address.startswith('http'): address = 'http://' + address
    return address

def SD_api_address_update(address):
    
    global params
    
    msg = "✔️ SD API is found on:"
    address = filter_address(address)
    params.update({"address": address})
    try:
        response = requests.get(url=f'{params["address"]}/sdapi/v1/sd-models')
        response.raise_for_status()
        r = response.json()
    except:
        msg = "❌ No SD API endpoint on:"

    return gr.Textbox.update(label=msg)



def ui():

    # Gradio elements
    # gr.Markdown('### Stable Diffusion API Pictures') # Currently the name of extension is shown as the title
    with gr.Accordion("Parameters", open=True):
        with gr.Row():
            address = gr.Textbox(placeholder=params['address'], value=params['address'], label='Auto1111\'s WebUI address')
            mode = gr.Dropdown(["Manual", "Immersive/Interactive", "Picturebook/Adventure"], value="Manual", label="Mode of operation", type="index")
            with gr.Column(scale=1, min_width=300):
                manage_VRAM = gr.Checkbox(value=params['manage_VRAM'], label='Manage VRAM')
                save_img = gr.Checkbox(value=params['save_img'], label='Keep original images and use them in chat')

            force_pic = gr.Button("Force the picture response")
            suppr_pic = gr.Button("Suppress the picture response")

        with gr.Accordion("Generation parameters", open=False):
            prompt_prefix = gr.Textbox(placeholder=params['prompt_prefix'], value=params['prompt_prefix'], label='Prompt Prefix (best used to describe the look of the character)')
            with gr.Row():
                with gr.Column():
                    negative_prompt = gr.Textbox(placeholder=params['negative_prompt'], value=params['negative_prompt'], label='Negative Prompt')
                    sampler_name = gr.Textbox(placeholder=params['sampler_name'], value=params['sampler_name'], label='Sampler')
                with gr.Column():
                    width = gr.Slider(256,704,value=params['width'],step=64,label='Width')
                    height = gr.Slider(256,704,value=params['height'],step=64,label='Height')
            with gr.Row():
                steps = gr.Number(label="Steps:", value=params['steps'])
                seed = gr.Number(label="Seed:", value=params['seed'])
                cfg_scale = gr.Number(label="CFG Scale:", value=params['cfg_scale'])
    
    # Event functions to update the parameters in the backend
    address.change(lambda x: params.update({"address": filter_address(x)}), address, None)
    mode.select(lambda x: params.update({"mode": x }), mode, None)
    mode.select(lambda x: toggle_generation(x>1), inputs=mode, outputs=None)
    manage_VRAM.change(lambda x: params.update({"manage_VRAM": x}), manage_VRAM, None)
    manage_VRAM.change(lambda x: give_VRAM_priority('set' if x else 'reset'), inputs=manage_VRAM, outputs=None)
    save_img.change(lambda x: params.update({"save_img": x}), save_img, None)
    
    address.submit(fn=SD_api_address_update, inputs=address, outputs=address)
    prompt_prefix.change(lambda x: params.update({"prompt_prefix": x}), prompt_prefix, None)
    negative_prompt.change(lambda x: params.update({"negative_prompt": x}), negative_prompt, None)
    width.change(lambda x: params.update({"width": x}), width, None)
    height.change(lambda x: params.update({"height": x}), height, None)
    
    sampler_name.change(lambda x: params.update({"sampler_name": x}), sampler_name, None)
    steps.change(lambda x: params.update({"steps": x}), steps, None)
    seed.change(lambda x: params.update({"seed": x}), seed, None)
    cfg_scale.change(lambda x: params.update({"cfg_scale": x}), cfg_scale, None)

    force_pic.click(lambda x: toggle_generation(True), inputs=force_pic, outputs=None)
    suppr_pic.click(lambda x: toggle_generation(False), inputs=suppr_pic, outputs=None)