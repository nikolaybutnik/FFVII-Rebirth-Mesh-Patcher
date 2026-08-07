cmake_minimum_required(VERSION 3.14)
project(ffvii_rebirth_mesh_patcher_linux_deps)

include(FetchContent)

FetchContent_Declare(
  linoodle
  GIT_REPOSITORY https://github.com/McSimp/linoodle.git
  GIT_TAG        90b8d825f7f89272f03f52b5d1db4708e07eb83f
  GIT_SUBMODULES_RECURSE TRUE
  UPDATE_DISCONNECTED TRUE
)

set(BUILD_COMMAND_LINE_TOOLS OFF CACHE BOOL "" FORCE)

FetchContent_MakeAvailable(linoodle)

target_compile_options(linoodle PRIVATE -include utility)

set_target_properties(linoodle PROPERTIES
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
)
