import traceback

import folder_paths
import locale
import concurrent
import nodes
import os
import sys
import re
import git

from server import PromptServer
import manager_core as core
import cm_global

from . import manager_ext_core as ext_core
from . import manager_ext_util

print(f"### Loading: ComfyUI-Manager (ext) ({ext_core.version_str})")

comfy_ui_hash = "-"

SECURITY_MESSAGE_MIDDLE_OR_BELOW = f"ERROR: To use this action, a security_level of `middle or below` is required. Please contact the administrator.\nReference: https://github.com/ltdrdata/ComfyUI-Manager#security-policy"
SECURITY_MESSAGE_NORMAL_MINUS = f"ERROR: To use this feature, you must either set '--listen' to a local IP and set the security level to 'normal-' or lower, or set the security level to 'middle' or 'weak'. Please contact the administrator.\nReference: https://github.com/ltdrdata/ComfyUI-Manager#security-policy"
SECURITY_MESSAGE_GENERAL = f"ERROR: This installation is not allowed in this security_level. Please contact the administrator.\nReference: https://github.com/ltdrdata/ComfyUI-Manager#security-policy"

routes = PromptServer.instance.routes


def handle_stream(stream, prefix):
    stream.reconfigure(encoding=locale.getpreferredencoding(), errors='replace')
    for msg in stream:
        if prefix == '[!]' and ('it/s]' in msg or 's/it]' in msg) and ('%|' in msg or 'it [' in msg):
            if msg.startswith('100%'):
                print('\r' + msg, end="", file=sys.stderr),
            else:
                print('\r' + msg[:-1], end="", file=sys.stderr),
        else:
            if prefix == '[!]':
                print(prefix, msg, end="", file=sys.stderr)
            else:
                print(prefix, msg, end="")


from comfy.cli_args import args
import latent_preview


is_local_mode = args.listen.startswith('127.') or args.listen.startswith('local.')


def is_allowed_security_level(level):
    if level == 'block':
        return False
    elif level == 'high':
        if is_local_mode:
            return core.get_config()['security_level'].lower() in ['weak', 'normal-']
        else:
            return core.get_config()['security_level'].lower() == 'weak'
    elif level == 'middle':
        return core.get_config()['security_level'].lower() in ['weak', 'normal', 'normal-']
    else:
        return True


async def get_risky_level(files, pip_packages):
    json_data1 = await core.get_data_by_mode('local', 'custom-node-list.json')
    json_data2 = await core.get_data_by_mode('cache', 'custom-node-list.json', channel_url='https://github.com/ltdrdata/ComfyUI-Manager/raw/main')

    all_urls = set()
    for x in json_data1['custom_nodes'] + json_data2['custom_nodes']:
        all_urls.update(x['files'])

    for x in files:
        if x not in all_urls:
            return "high"

    all_pip_packages = set()
    for x in json_data1['custom_nodes'] + json_data2['custom_nodes']:
        if "pip" in x:
            all_pip_packages.update(x['pip'])

    for p in pip_packages:
        if p not in all_pip_packages:
            return "block"

    return "middle"


from manager_downloader import download_url

components_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'components'))


def set_preview_method(method):
    if method == 'auto':
        args.preview_method = latent_preview.LatentPreviewMethod.Auto
    elif method == 'latent2rgb':
        args.preview_method = latent_preview.LatentPreviewMethod.Latent2RGB
    elif method == 'taesd':
        args.preview_method = latent_preview.LatentPreviewMethod.TAESD
    else:
        args.preview_method = latent_preview.LatentPreviewMethod.NoPreviews

    ext_core.get_config()['preview_method'] = args.preview_method


set_preview_method(ext_core.get_config()['preview_method'])


def set_default_ui_mode(mode):
    ext_core.get_config()['default_ui'] = mode


def set_component_policy(mode):
    ext_core.get_config()['component_policy'] = mode


def set_double_click_policy(mode):
    ext_core.get_config()['double_click_policy'] = mode


# Expand Server api
import server
from aiohttp import web
import aiohttp
import json
import zipfile
import urllib.request


def check_state_of_git_node_pack(node_packs, do_fetch=False, do_update_check=True, do_update=False):
    if do_fetch:
        print("Start fetching...", end="")
    elif do_update:
        print("Start updating...", end="")
    elif do_update_check:
        print("Start update check...", end="")

    def process_custom_node(item):
        core.check_state_of_git_node_pack_single(item, do_fetch, do_update_check, do_update)

    with concurrent.futures.ThreadPoolExecutor(4) as executor:
        for k, v in node_packs.items():
            if v.get('active_version') in ['unknown', 'nightly']:
                executor.submit(process_custom_node, v)

    if do_fetch:
        print(f"\x1b[2K\rFetching done.")
    elif do_update:
        update_exists = any(item.get('updatable', False) for item in node_packs.values())
        if update_exists:
            print(f"\x1b[2K\rUpdate done.")
        else:
            print(f"\x1b[2K\rAll extensions are already up-to-date.")
    elif do_update_check:
        print(f"\x1b[2K\rUpdate check done.")


def nickname_filter(json_obj):
    preemptions_map = {}

    for k, x in json_obj.items():
        if 'preemptions' in x[1]:
            for y in x[1]['preemptions']:
                preemptions_map[y] = k
        elif k.endswith("/ComfyUI"):
            for y in x[0]:
                preemptions_map[y] = k

    updates = {}
    for k, x in json_obj.items():
        removes = set()
        for y in x[0]:
            k2 = preemptions_map.get(y)
            if k2 is not None and k != k2:
                removes.add(y)

        if len(removes) > 0:
            updates[k] = [y for y in x[0] if y not in removes]

    for k, v in updates.items():
        json_obj[k][0] = v

    return json_obj


@routes.get("/customnode/getmappings")
async def fetch_customnode_mappings(request):
    """
    provide unified (node -> node pack) mapping list
    """
    mode = request.rel_url.query["mode"]

    nickname_mode = False
    if mode == "nickname":
        mode = "local"
        nickname_mode = True

    json_obj = await core.get_data_by_mode(mode, 'extension-node-map.json')
    json_obj = core.map_to_unified_keys(json_obj)

    if nickname_mode:
        json_obj = nickname_filter(json_obj)

    all_nodes = set()
    patterns = []
    for k, x in json_obj.items():
        all_nodes.update(set(x[0]))

        if 'nodename_pattern' in x[1]:
            patterns.append((x[1]['nodename_pattern'], x[0]))

    missing_nodes = set(nodes.NODE_CLASS_MAPPINGS.keys()) - all_nodes

    for x in missing_nodes:
        for pat, item in patterns:
            if re.match(pat, x):
                item.append(x)

    return web.json_response(json_obj, content_type='application/json')


@routes.get("/customnode/fetch_updates")
async def fetch_updates(request):
    try:
        if request.rel_url.query["mode"] == "local":
            channel = 'local'
        else:
            channel = core.get_config()['channel_url']

        await core.unified_manager.reload(request.rel_url.query["mode"])
        await core.unified_manager.get_custom_nodes(channel, request.rel_url.query["mode"])

        res = core.unified_manager.fetch_or_pull_git_repo(is_pull=False)

        for x in res['failed']:
            print(f"FETCH FAILED: {x}")

        print("\nDone.")

        if len(res['updated']) > 0:
            return web.Response(status=201)

        return web.Response(status=200)
    except:
        traceback.print_exc()
        return web.Response(status=400)


@routes.get("/customnode/update_all")
async def update_all(request):
    if not is_allowed_security_level('middle'):
        print(SECURITY_MESSAGE_MIDDLE_OR_BELOW)
        return web.Response(status=403)

    try:
        core.save_snapshot_with_postfix('autosave')

        if request.rel_url.query["mode"] == "local":
            channel = 'local'
        else:
            channel = core.get_config()['channel_url']

        await core.unified_manager.reload(request.rel_url.query["mode"])
        await core.unified_manager.get_custom_nodes(channel, request.rel_url.query["mode"])

        updated_cnr = []
        for k, v in core.unified_manager.active_nodes.items():
            if v[0] != 'nightly':
                res = core.unified_manager.unified_update(k, v[0])
                if res.action == 'switch-cnr' and res:
                    updated_cnr.append(k)

        res = core.unified_manager.fetch_or_pull_git_repo(is_pull=True)

        res['updated'] += updated_cnr

        for x in res['failed']:
            print(f"PULL FAILED: {x}")

        if len(res['updated']) == 0 and len(res['failed']) == 0:
            status = 200
        else:
            status = 201

        print(f"\nDone.")
        return web.json_response(res, status=status, content_type='application/json')
    except:
        traceback.print_exc()
        return web.Response(status=400)
    finally:
        core.clear_pip_cache()


def convert_markdown_to_html(input_text):
    pattern_a = re.compile(r'\[a/([^]]+)\]\(([^)]+)\)')
    pattern_w = re.compile(r'\[w/([^]]+)\]')
    pattern_i = re.compile(r'\[i/([^]]+)\]')
    pattern_bold = re.compile(r'\*\*([^*]+)\*\*')
    pattern_white = re.compile(r'%%([^*]+)%%')

    def replace_a(match):
        return f"<a href='{match.group(2)}' target='blank'>{match.group(1)}</a>"

    def replace_w(match):
        return f"<p class='cm-warn-note'>{match.group(1)}</p>"

    def replace_i(match):
        return f"<p class='cm-info-note'>{match.group(1)}</p>"

    def replace_bold(match):
        return f"<B>{match.group(1)}</B>"

    def replace_white(match):
        return f"<font color='white'>{match.group(1)}</font>"

    input_text = input_text.replace('\\[', '&#91;').replace('\\]', '&#93;').replace('<', '&lt;').replace('>', '&gt;')

    result_text = re.sub(pattern_a, replace_a, input_text)
    result_text = re.sub(pattern_w, replace_w, result_text)
    result_text = re.sub(pattern_i, replace_i, result_text)
    result_text = re.sub(pattern_bold, replace_bold, result_text)
    result_text = re.sub(pattern_white, replace_white, result_text)

    return result_text.replace("\n", "<BR>")


def populate_markdown(x):
    if 'description' in x:
        x['description'] = convert_markdown_to_html(manager_ext_util.sanitize_tag(x['description']))

    if 'name' in x:
        x['name'] = manager_ext_util.sanitize_tag(x['name'])

    if 'title' in x:
        x['title'] = manager_ext_util.sanitize_tag(x['title'])


@routes.get("/customnode/getlist")
async def fetch_customnode_list(request):
    """
    provide unified custom node list
    """
    if "skip_update" in request.rel_url.query and request.rel_url.query["skip_update"] == "true":
        skip_update = True
    else:
        skip_update = False

    if request.rel_url.query["mode"] == "local":
        channel = 'local'
    else:
        channel = core.get_config()['channel_url']

    node_packs = await core.get_unified_total_nodes(channel, request.rel_url.query["mode"])
    json_obj_github = core.get_data_by_mode(request.rel_url.query["mode"], 'github-stats.json', 'default')
    json_obj_extras = core.get_data_by_mode(request.rel_url.query["mode"], 'extras.json', 'default')

    core.populate_github_stats(node_packs, await json_obj_github)
    core.populate_favorites(node_packs, await json_obj_extras)

    check_state_of_git_node_pack(node_packs, False, do_update_check=not skip_update)

    for v in node_packs.values():
        populate_markdown(v)

    if channel != 'local':
        found = 'custom'

        for name, url in core.get_channel_dict().items():
            if url == channel:
                found = name
                break

        channel = found

    result = dict(channel=channel, node_packs=node_packs)

    return web.json_response(result, content_type='application/json')


@routes.get("/customnode/alternatives")
async def fetch_customnode_alternatives(request):
    alter_json = await core.get_data_by_mode(request.rel_url.query["mode"], 'alter-list.json')

    res = {}

    for item in alter_json['items']:
        populate_markdown(item)
        res[item['id']] = item

    res = core.map_to_unified_keys(res)

    return web.json_response(res, content_type='application/json')


@PromptServer.instance.routes.get("/snapshot/getlist")
async def get_snapshot_list(request):
    snapshots_directory = os.path.join(core.comfyui_manager_path, 'snapshots')
    items = [f[:-5] for f in os.listdir(snapshots_directory) if f.endswith('.json')]
    items.sort(reverse=True)
    return web.json_response({'items': items}, content_type='application/json')


@routes.get("/snapshot/remove")
async def remove_snapshot(request):
    if not is_allowed_security_level('middle'):
        print(SECURITY_MESSAGE_MIDDLE_OR_BELOW)
        return web.Response(status=403)
    
    try:
        target = request.rel_url.query["target"]

        path = os.path.join(core.comfyui_manager_path, 'snapshots', f"{target}.json")
        if os.path.exists(path):
            os.remove(path)

        return web.Response(status=200)
    except:
        return web.Response(status=400)


@routes.get("/snapshot/get_current")
async def get_current_snapshot_api(request):
    try:
        return web.json_response(core.get_current_snapshot(), content_type='application/json')
    except:
        return web.Response(status=400)


def unzip_install(files):
    temp_filename = 'manager-temp.zip'
    for url in files:
        if url.endswith("/"):
            url = url[:-1]
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}

            req = urllib.request.Request(url, headers=headers)
            response = urllib.request.urlopen(req)
            data = response.read()

            with open(temp_filename, 'wb') as f:
                f.write(data)

            with zipfile.ZipFile(temp_filename, 'r') as zip_ref:
                zip_ref.extractall(core.custom_nodes_path)

            os.remove(temp_filename)
        except Exception as e:
            print(f"Install(unzip) error: {url} / {e}", file=sys.stderr)
            return False

    print("Installation was successful.")
    return True


def download_url_with_agent(url, save_path):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}

        req = urllib.request.Request(url, headers=headers)
        response = urllib.request.urlopen(req)
        data = response.read()

        if not os.path.exists(os.path.dirname(save_path)):
            os.makedirs(os.path.dirname(save_path))

        with open(save_path, 'wb') as f:
            f.write(data)

    except Exception as e:
        print(f"Download error: {url} / {e}", file=sys.stderr)
        return False

    print("Installation was successful.")
    return True


@routes.post("/customnode/install/git_url")
async def install_custom_node_git_url(request):
    if not is_allowed_security_level('high'):
        print(SECURITY_MESSAGE_NORMAL_MINUS)
        return web.Response(status=403)

    url = await request.text()
    res = await core.gitclone_install(url)

    if res.action == 'skip':
        print(f"Already installed: '{res.target}'")
        return web.Response(status=200)
    elif res.result:
        print(f"After restarting ComfyUI, please refresh the browser.")
        return web.Response(status=200)

    print(res.msg)
    return web.Response(status=400)


@routes.post("/customnode/install/pip")
async def install_pip(request):
    if not is_allowed_security_level('high'):
        print(SECURITY_MESSAGE_NORMAL_MINUS)
        return web.Response(status=403)

    packages = await request.text()
    core.pip_install(packages.split(' '))

    return web.Response(status=200)


@routes.get("/comfyui_manager/update_comfyui")
async def update_comfyui(request):
    print(f"Update ComfyUI")

    try:
        repo_path = os.path.dirname(folder_paths.__file__)
        res = core.update_path(repo_path)
        if res == "fail":
            print(f"ComfyUI update fail: The installed ComfyUI does not have a Git repository.")
            return web.Response(status=400)
        elif res == "updated":
            return web.Response(status=201)
        else:  # skipped
            return web.Response(status=200)
    except Exception as e:
        print(f"ComfyUI update fail: {e}", file=sys.stderr)

    return web.Response(status=400)


@routes.get("/comfyui_manager/comfyui_switch_version")
async def comfyui_switch_version(request):
    try:
        if "ver" in request.rel_url.query:
            core.switch_comfyui(request.rel_url.query['ver'])

        return web.Response(status=200)
    except Exception as e:
        print(f"ComfyUI update fail: {e}", file=sys.stderr)

    return web.Response(status=400)


@PromptServer.instance.routes.get("/manager/preview_method")
async def preview_method(request):
    if "value" in request.rel_url.query:
        set_preview_method(request.rel_url.query['value'])
        ext_core.write_config()
    else:
        return web.Response(text=ext_core.get_current_preview_method(), status=200)

    return web.Response(status=200)


@routes.get("/manager/default_ui")
async def default_ui_mode(request):
    if "value" in request.rel_url.query:
        set_default_ui_mode(request.rel_url.query['value'])
        ext_core.write_config()
    else:
        return web.Response(text=ext_core.get_config()['default_ui'], status=200)

    return web.Response(status=200)


@routes.get("/manager/component/policy")
async def component_policy(request):
    if "value" in request.rel_url.query:
        set_component_policy(request.rel_url.query['value'])
        ext_core.write_config()
    else:
        return web.Response(text=ext_core.get_config()['component_policy'], status=200)

    return web.Response(status=200)


@routes.get("/manager/dbl_click/policy")
async def dbl_click_policy(request):
    if "value" in request.rel_url.query:
        set_double_click_policy(request.rel_url.query['value'])
        ext_core.write_config()
    else:
        return web.Response(text=ext_core.get_config()['double_click_policy'], status=200)

    return web.Response(status=200)


def add_target_blank(html_text):
    pattern = r'(<a\s+href="[^"]*"\s*[^>]*)(>)'

    def add_target(match):
        if 'target=' not in match.group(1):
            return match.group(1) + ' target="_blank"' + match.group(2)
        return match.group(0)

    modified_html = re.sub(pattern, add_target, html_text)

    return modified_html


@routes.get("/manager/notice")
async def get_notice(request):
    url = "github.com"
    path = "/ltdrdata/ltdrdata.github.io/wiki/News"

    async with aiohttp.ClientSession(trust_env=True, connector=aiohttp.TCPConnector(verify_ssl=False)) as session:
        async with session.get(f"https://{url}{path}") as response:
            if response.status == 200:
                # html_content = response.read().decode('utf-8')
                html_content = await response.text()

                pattern = re.compile(r'<div class="markdown-body">([\s\S]*?)</div>')
                match = pattern.search(html_content)

                if match:
                    markdown_content = match.group(1)
                    if core.comfyui_tag:
                        markdown_content += f"<HR>ComfyUI: <b>{core.comfyui_tag}</b><BR>Commit Date: {core.comfy_ui_commit_datetime.date()}"
                    else:
                        markdown_content += f"<HR>ComfyUI: <b>{core.comfy_ui_revision}</b>[{comfy_ui_hash[:6]}]({core.comfy_ui_commit_datetime.date()})"
                    # markdown_content += f"<BR>&nbsp; &nbsp; &nbsp; &nbsp; &nbsp;()"
                    markdown_content += f"<BR>manager-core: {core.version_str}"
                    markdown_content += f"<BR>manager-ext: {ext_core.version_str}"

                    markdown_content = add_target_blank(markdown_content)

                    try:
                        if core.comfy_ui_required_commit_datetime.date() > core.comfy_ui_commit_datetime.date():
                            markdown_content = f'<P style="text-align: center; color:red; background-color:white; font-weight:bold">Your ComfyUI is too OUTDATED!!!</P>' + markdown_content
                    except:
                        pass

                    return web.Response(text=markdown_content, status=200)
                else:
                    return web.Response(text="Unable to retrieve Notice", status=200)
            else:
                return web.Response(text="Unable to retrieve Notice", status=200)


@routes.get("/manager/reboot")
def restart(self):
    if not is_allowed_security_level('middle'):
        print(SECURITY_MESSAGE_MIDDLE_OR_BELOW)
        return web.Response(status=403)

    try:
        sys.stdout.close_log()
    except Exception as e:
        pass

    if '__COMFY_CLI_SESSION__' in os.environ:
        with open(os.path.join(os.environ['__COMFY_CLI_SESSION__'] + '.reboot'), 'w') as file:
            pass

        print(f"\nRestarting...\n\n")
        exit(0)

    print(f"\nRestarting... [Legacy Mode]\n\n")
    if sys.platform.startswith('win32'):
        return os.execv(sys.executable, ['"' + sys.executable + '"', '"' + sys.argv[0] + '"'] + sys.argv[1:])
    else:
        return os.execv(sys.executable, [sys.executable] + sys.argv)


def sanitize_filename(input_string):
    result_string = re.sub(r'[^a-zA-Z0-9_]', '_', input_string)
    return result_string


@routes.post("/manager/component/save")
async def save_component(request):
    try:
        data = await request.json()
        name = data['name']
        workflow = data['workflow']

        if not os.path.exists(components_path):
            os.mkdir(components_path)

        if 'packname' in workflow and workflow['packname'] != '':
            sanitized_name = sanitize_filename(workflow['packname']) + '.pack'
        else:
            sanitized_name = sanitize_filename(name) + '.json'

        filepath = os.path.join(components_path, sanitized_name)
        components = {}
        if os.path.exists(filepath):
            with open(filepath) as f:
                components = json.load(f)

        components[name] = workflow

        with open(filepath, 'w') as f:
            json.dump(components, f, indent=4, sort_keys=True)
        return web.Response(text=filepath, status=200)
    except:
        return web.Response(status=400)


@routes.post("/manager/component/loads")
async def load_components(request):
    try:
        json_files = [f for f in os.listdir(components_path) if f.endswith('.json')]
        pack_files = [f for f in os.listdir(components_path) if f.endswith('.pack')]

        components = {}
        for json_file in json_files + pack_files:
            file_path = os.path.join(components_path, json_file)
            with open(file_path, 'r') as file:
                try:
                    # When there is a conflict between the .pack and the .json, the pack takes precedence and overrides.
                    components.update(json.load(file))
                except json.JSONDecodeError as e:
                    print(f"[ComfyUI-Manager] Error decoding component file in file {json_file}: {e}")

        return web.json_response(components)
    except Exception as e:
        print(f"[ComfyUI-Manager] failed to load components\n{e}")
        return web.Response(status=400)


args.enable_cors_header = "*"
if hasattr(PromptServer.instance, "app"):
    app = PromptServer.instance.app
    cors_middleware = server.create_cors_middleware(args.enable_cors_header)
    app.middlewares.append(cors_middleware)


def sanitize(data):
    return data.replace("<", "&lt;").replace(">", "&gt;")

if not os.path.exists(ext_core.manager_ext_config_path):
    ext_core.get_config()
    ext_core.write_config()

cm_global.register_extension('ComfyUI-Manager',
                             {'version': core.version,
                              'name': 'ComfyUI Manager (Extension)',
                              'nodes': {},
                              'description': 'ComfyUI-Manager (Extension)', })

cm_global.variables['manager-core.show_menu'] = False
