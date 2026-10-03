# Adds bench_varops and bench_varops_primitives to a Bitcoin Core build of the
# gsr branch without changing its sources. Configure Core with
#
#   cmake -B build -DBUILD_BENCH=ON \
#       -DCMAKE_PROJECT_BitcoinCore_INCLUDE=<varopsData>/bench/varops_bench.cmake
#
# Core includes this file right after its project() call, before its targets
# exist, so the benchmarks are added once Core's top-level directory has been
# processed. They link the libraries src/bench/ targets link and take Core's
# compiler flags from core_interface, so they compile as they would in-tree.

set(VAROPS_BENCH_DIR ${CMAKE_CURRENT_LIST_DIR})

function(add_varops_benchmarks)
  # src/CMakeLists.txt sets these for its own directory only.
  set(output_dir ${PROJECT_BINARY_DIR}/bin)
  set(include_dirs ${PROJECT_BINARY_DIR}/src ${PROJECT_SOURCE_DIR}/src)

  add_executable(bench_varops
    ${VAROPS_BENCH_DIR}/bench_varops.cpp
    ${PROJECT_SOURCE_DIR}/src/bench/bench.cpp
    ${PROJECT_SOURCE_DIR}/src/bench/nanobench.cpp
  )
  add_executable(bench_varops_primitives
    ${VAROPS_BENCH_DIR}/bench_varops_primitives.cpp
  )
  foreach(target bench_varops bench_varops_primitives)
    target_include_directories(${target} PRIVATE ${include_dirs})
    target_link_libraries(${target}
      core_interface
      test_util
      bitcoin_node
      secp256k1
      Boost::headers
    )
    set_target_properties(${target} PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${output_dir})
    add_windows_application_manifest(${target})
  endforeach()
endfunction()

cmake_language(DEFER CALL add_varops_benchmarks)
