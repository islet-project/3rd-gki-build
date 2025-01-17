# Copyright (C) 2022 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Support `compile_commands.json`."""

load("//build/kernel/kleaf:hermetic_tools.bzl", "HermeticToolsInfo")
load(
    ":common_providers.bzl",
    "KernelBuildInfo",
)

def _kernel_compile_commands_transition_impl(_settings, _attr):
    return {"//build/kernel/kleaf/impl:build_compile_commands": True}

_kernel_compile_commands_transition = transition(
    implementation = _kernel_compile_commands_transition_impl,
    inputs = [],
    outputs = ["//build/kernel/kleaf/impl:build_compile_commands"],
)

def _kernel_compile_commands_impl(ctx):
    compile_commands_with_vars = ctx.attr.kernel_build[KernelBuildInfo].compile_commands_with_vars
    compile_commands_out_dir = ctx.attr.kernel_build[KernelBuildInfo].compile_commands_out_dir

    script = ctx.actions.declare_file(ctx.attr.name + ".sh")
    script_content = ctx.attr._hermetic_tools[HermeticToolsInfo].run_setup + """
        OUTPUT=${{1:-${{BUILD_WORKSPACE_DIRECTORY}}/compile_commands.json}}
        sed -e "s:\\${{OUT_DIR}}:${{BUILD_WORKSPACE_DIRECTORY}}/{compile_commands_out_dir}:g;s:\\${{ROOT_DIR}}:${{BUILD_WORKSPACE_DIRECTORY}}:g" \\
            {compile_commands_with_vars} > ${{OUTPUT}}
        echo "Written to ${{OUTPUT}}"
    """.format(
        compile_commands_with_vars = compile_commands_with_vars.short_path,
        compile_commands_out_dir = compile_commands_out_dir.path,
    )
    ctx.actions.write(script, script_content, is_executable = True)

    direct_runfiles = [compile_commands_with_vars]
    direct_runfiles += ctx.attr._hermetic_tools[HermeticToolsInfo].deps

    return DefaultInfo(
        executable = script,
        runfiles = ctx.runfiles(files = direct_runfiles),
    )

kernel_compile_commands = rule(
    implementation = _kernel_compile_commands_impl,
    doc = """Define an executable that creates `compile_commands.json` from a `kernel_build`.""",
    attrs = {
        "kernel_build": attr.label(
            mandatory = True,
            doc = "The `kernel_build` rule to extract from.",
            providers = [KernelBuildInfo],
        ),
        "_hermetic_tools": attr.label(default = "//build/kernel:hermetic-tools", providers = [HermeticToolsInfo]),
        # Allow any package to use kernel_compile_commands because it is a public API.
        # The ACK source tree may be checked out anywhere; it is not necessarily //common
        "_allowlist_function_transition": attr.label(
            default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
        ),
    },
    executable = True,
    cfg = _kernel_compile_commands_transition,
)
