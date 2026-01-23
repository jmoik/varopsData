#!/usr/bin/env python3

import csv
import sys
import statistics
from pathlib import Path
from collections import defaultdict
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

SCHNORR_BASELINE = 80000
AVERAGE_CUTOFF = SCHNORR_BASELINE / 2


def extract_opcode(name: str) -> str:
    """Extract the core opcode from a benchmark name like '3DUP_HASH256_DROP_...' -> 'HASH256'"""
    # Known opcodes (including ones with numbers)
    known_opcodes = {'HASH256', 'SHA256', 'HASH160', 'SHA1', 'RIPEMD160', 'LSHIFT', 'RSHIFT',
                     'AND', 'OR', 'XOR', 'NOT', 'CAT', 'SUBSTR', 'MUL', 'DIV', 'MOD'}
    parts = name.split('_')
    for part in parts:
        if part in known_opcodes:
            return part
        if part.endswith('DUP') or part == 'DUP':
            continue
        if part == 'DROP':
            continue
        if any(c.isdigit() for c in part):
            continue
        if part.lower() == 'shift':
            continue
        return part
    return name


def extract_machine_info(filepath: str) -> dict:
    info = {'cpu': 'Unknown', 'arch': 'Unknown', 'file': Path(filepath).name}
    with open(filepath, 'r') as f:
        for line in f:
            if not line.startswith('#'):
                break
            if 'CPU:' in line:
                info['cpu'] = line.split('CPU:')[1].strip()
            elif 'Architecture:' in line:
                info['arch'] = line.split('Architecture:')[1].strip()
    return info


def parse_csv(filepath: str) -> tuple[list[dict], dict]:
    results = []
    machine_info = extract_machine_info(filepath)
    
    with open(filepath, 'r') as f:
        lines = [line for line in f if not line.startswith('#')]
    
    reader = csv.DictReader(lines)
    for row in reader:
        results.append({
            'rank': int(row['Rank']),
            'name': row['Name'],
            'seconds': float(row['Seconds']),
            'schnorr_equivalents': float(row['Schnorr_Equivalents']),
            'varops_percentage': float(row['Varops_Percentage']),
            'is_gsr_only': row['Is_GSR_Only'].lower() == 'true'
        })
    return results, machine_info


def parse_multiple_csvs(filepaths: list[str]) -> tuple[list[dict], list[dict]]:
    all_machine_data = []
    benchmark_data = defaultdict(lambda: {'seconds': [], 'schnorr_equivalents': [], 'varops_percentage': [], 'is_gsr_only': None})
    
    for filepath in filepaths:
        results, machine_info = parse_csv(filepath)
        all_machine_data.append((results, machine_info))
        
        for r in results:
            name = r['name']
            benchmark_data[name]['seconds'].append(r['seconds'])
            benchmark_data[name]['schnorr_equivalents'].append(r['schnorr_equivalents'])
            benchmark_data[name]['varops_percentage'].append(r['varops_percentage'])
            if benchmark_data[name]['is_gsr_only'] is None:
                benchmark_data[name]['is_gsr_only'] = r['is_gsr_only']
    
    averaged_results = []
    for name, data in benchmark_data.items():
        n = len(data['seconds'])
        avg_seconds = sum(data['seconds']) / n
        std_seconds = statistics.stdev(data['seconds']) if n > 1 else 0

        averaged_results.append({
            'name': name,
            'seconds': avg_seconds,
            'seconds_std': std_seconds,
            'schnorr_equivalents': sum(data['schnorr_equivalents']) / n,
            'varops_percentage': sum(data['varops_percentage']) / n,
            'is_gsr_only': data['is_gsr_only'],
            'seconds_all': data['seconds'],
            'schnorr_equivalents_all': data['schnorr_equivalents'],
        })
    
    return averaged_results, all_machine_data


def get_schnorr_baseline(results: list[dict]) -> dict | None:
    for r in results:
        if r['name'] == 'Schnorr signature validation':
            return r
    return None


def analyze_results(results: list[dict]) -> tuple[list[dict], list[dict]]:
    current_script = []
    gsr_added = []
    
    for r in results:
        if r['name'] == 'Schnorr signature validation':
            continue
        if r['is_gsr_only']:
            gsr_added.append(r)
        else:
            current_script.append(r)
    
    current_script.sort(key=lambda x: x['seconds'], reverse=True)
    gsr_added.sort(key=lambda x: x['seconds'], reverse=True)
    
    return current_script, gsr_added


def print_summary(current_script: list[dict], gsr_added: list[dict], schnorr: dict | None, num_machines: int):
    schnorr_time = schnorr['seconds'] if schnorr else 0
    
    print("\nBENCHMARK ANALYSIS")
    print(f"Averaged across {num_machines} machine(s)\n")
    
    if schnorr:
        print(f"Schnorr baseline: {schnorr_time:.3f}s")
    
    print("\nCURRENT BITCOIN SCRIPT")
    if current_script:
        print(f"{'Rank':<6} {'Operation':<45} {'Time (s)':<12} {'vs Schnorr':<15} {'Varops %'}")
        for i, r in enumerate(current_script[:10], 1):
            ratio = r['seconds'] / schnorr_time if schnorr_time > 0 else 0
            print(f"{i:<6} {r['name']:<45} {r['seconds']:<12.3f} {ratio:<15.2f}x {r['varops_percentage']:.1f}%")
        
        worst = current_script[0]
        print(f"\nWorst case: {worst['name']}")
        std = worst.get('seconds_std', 0)
        if std > 0:
            print(f"Time: {worst['seconds']:.3f} ± {std:.3f}s")
        else:
            print(f"Time: {worst['seconds']:.3f}s")
        if schnorr_time > 0:
            print(f"Ratio: {worst['seconds'] / schnorr_time:.2f}x Schnorr baseline")
    else:
        print("No current script operations found")
    
    print("\nNEW GSR OPERATIONS")
    if gsr_added:
        print(f"{'Rank':<6} {'Operation':<45} {'Time (s)':<12} {'vs Schnorr':<15} {'Varops %'}")
        for i, r in enumerate(gsr_added[:10], 1):
            ratio = r['seconds'] / schnorr_time if schnorr_time > 0 else 0
            print(f"{i:<6} {r['name']:<45} {r['seconds']:<12.3f} {ratio:<15.2f}x {r['varops_percentage']:.1f}%")
        
        worst = gsr_added[0]
        print(f"\nWorst case: {worst['name']}")
        std = worst.get('seconds_std', 0)
        if std > 0:
            print(f"Time: {worst['seconds']:.3f} ± {std:.3f}s")
        else:
            print(f"Time: {worst['seconds']:.3f}s")
        if schnorr_time > 0:
            print(f"Ratio: {worst['seconds'] / schnorr_time:.2f}x Schnorr baseline")
    else:
        print("No new GSR operations found")
    
    print("\nCOMPARISON")
    if current_script and gsr_added:
        curr_worst = current_script[0]['seconds']
        gsr_worst = gsr_added[0]['seconds']
        print(f"Worst current script: {curr_worst:.3f}s ({current_script[0]['name']})")
        print(f"Worst new GSR: {gsr_worst:.3f}s ({gsr_added[0]['name']})")
        print(f"Difference: {gsr_worst/curr_worst:.2f}x")
    print()


def create_averaged_visualization(current_script: list[dict], gsr_added: list[dict], schnorr: dict | None,
                                   num_machines: int, output_path: str):
    schnorr_time = schnorr['seconds'] if schnorr else 1

    curr_top = [r for r in current_script if r['schnorr_equivalents'] >= AVERAGE_CUTOFF]
    gsr_top = [r for r in gsr_added if r['schnorr_equivalents'] >= AVERAGE_CUTOFF]

    max_operations = max(len(curr_top), len(gsr_top))
    fig_height = max(10, min(30, max_operations * 0.6))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, fig_height))
    subtitle = f'All individual data points across {num_machines} machine(s) - Operations with average >= {AVERAGE_CUTOFF:,.0f} Schnorr equivalents'
    fig.suptitle(f'Worst Case Block Sized Script: Current Bitcoin Script vs New GSR Operations\n({subtitle})',
                 fontsize=14, fontweight='bold')

    curr_color = '#3498db'
    gsr_color = '#27ae60'
    schnorr_color = '#e74c3c'

    if curr_top:
        names = [r['name'][:40] + '...' if len(r['name']) > 40 else r['name'] for r in curr_top]

        for i, r in enumerate(curr_top):
            times_all = r.get('seconds_all', [r['seconds']])
            y_pos = [i] * len(times_all)

            ax1.scatter(times_all, y_pos, color=curr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5)
            ax1.scatter(r['seconds'], i, color=curr_color, s=100, marker='D', edgecolors='black', linewidth=1, zorder=5)

        ax1.axvline(x=schnorr_time, color=schnorr_color, linestyle='--', linewidth=2, label=f'Schnorr baseline ({schnorr_time:.2f}s)')
        # Add legend entries for markers
        ax1.scatter([], [], color=curr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5, label='Individual machines')
        ax1.scatter([], [], color=curr_color, s=100, marker='D', edgecolors='black', linewidth=1, label='Mean across machines')
        ax1.set_yticks(range(len(names)))
        ax1.set_yticklabels(names, fontsize=8)
        ax1.set_xlabel('Execution Time (seconds)')
        ax1.set_title('Current Bitcoin Script', fontsize=12, fontweight='bold', color=curr_color)
        ax1.invert_yaxis()
        ax1.legend(loc='lower right', fontsize=8)
        ax1.grid(axis='x', alpha=0.3)
    else:
        ax1.text(0.5, 0.5, 'No current script operations', ha='center', va='center', transform=ax1.transAxes)
        ax1.set_title('Current Bitcoin Script', fontsize=12, fontweight='bold', color=curr_color)

    if gsr_top:
        names = [r['name'][:40] + '...' if len(r['name']) > 40 else r['name'] for r in gsr_top]

        for i, r in enumerate(gsr_top):
            times_all = r.get('seconds_all', [r['seconds']])
            y_pos = [i] * len(times_all)

            ax2.scatter(times_all, y_pos, color=gsr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5)
            ax2.scatter(r['seconds'], i, color=gsr_color, s=100, marker='D', edgecolors='black', linewidth=1, zorder=5)

        ax2.axvline(x=schnorr_time, color=schnorr_color, linestyle='--', linewidth=2, label=f'Schnorr baseline ({schnorr_time:.2f}s)')
        # Add legend entries for markers
        ax2.scatter([], [], color=gsr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5, label='Individual machines')
        ax2.scatter([], [], color=gsr_color, s=100, marker='D', edgecolors='black', linewidth=1, label='Mean across machines')
        ax2.set_yticks(range(len(names)))
        ax2.set_yticklabels(names, fontsize=8)
        ax2.set_xlabel('Execution Time (seconds)')
        ax2.set_title('New Operations Added by GSR', fontsize=12, fontweight='bold', color=gsr_color)
        ax2.invert_yaxis()
        ax2.legend(loc='lower right', fontsize=8)
        ax2.grid(axis='x', alpha=0.3)
    else:
        ax2.text(0.5, 0.5, 'No new GSR operations', ha='center', va='center', transform=ax2.transAxes)
        ax2.set_title('New Operations Added by GSR', fontsize=12, fontweight='bold', color=gsr_color)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")


def create_schnorr_equivalents_visualization(current_script: list[dict], gsr_added: list[dict], schnorr: dict | None,
                                             num_machines: int, output_path: str):
    curr_top = [r for r in current_script if r['schnorr_equivalents'] >= AVERAGE_CUTOFF]
    gsr_top = [r for r in gsr_added if r['schnorr_equivalents'] >= AVERAGE_CUTOFF]

    max_operations = max(len(curr_top), len(gsr_top))
    fig_height = max(10, min(30, max_operations * 0.6))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, fig_height))
    subtitle = f'All individual data points across {num_machines} machine(s) - Operations with average >= {AVERAGE_CUTOFF:,.0f} Schnorr equivalents'
    fig.suptitle(f'Worst Case Block Sized Script: Current Bitcoin Script vs New GSR Operations\n({subtitle})',
                 fontsize=14, fontweight='bold')

    curr_color = '#3498db'
    gsr_color = '#27ae60'
    schnorr_color = '#e74c3c'

    if curr_top:
        names = [r['name'][:40] + '...' if len(r['name']) > 40 else r['name'] for r in curr_top]

        for i, r in enumerate(curr_top):
            schnorr_eqs_all = r.get('schnorr_equivalents_all', [r['schnorr_equivalents']])
            y_pos = [i] * len(schnorr_eqs_all)

            ax1.scatter(schnorr_eqs_all, y_pos, color=curr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5)
            ax1.scatter(r['schnorr_equivalents'], i, color=curr_color, s=100, marker='D', edgecolors='black', linewidth=1, zorder=5)

        ax1.axvline(x=SCHNORR_BASELINE, color=schnorr_color, linestyle='--', linewidth=2, label=f'Block limit ({SCHNORR_BASELINE:,} sigs)')
        # Add legend entries for markers
        ax1.scatter([], [], color=curr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5, label='Individual machines')
        ax1.scatter([], [], color=curr_color, s=100, marker='D', edgecolors='black', linewidth=1, label='Mean across machines')
        ax1.set_yticks(range(len(names)))
        ax1.set_yticklabels(names, fontsize=8)
        ax1.set_xlabel('Schnorr Signature Equivalents (per block)')
        ax1.set_title('Current Bitcoin Script', fontsize=12, fontweight='bold', color=curr_color)
        ax1.invert_yaxis()
        ax1.legend(loc='lower right', fontsize=8)
        ax1.grid(axis='x', alpha=0.3)
        ax1.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format(int(x), ',')))

    else:
        ax1.text(0.5, 0.5, 'No current script operations', ha='center', va='center', transform=ax1.transAxes)
        ax1.set_title('Current Bitcoin Script', fontsize=12, fontweight='bold', color=curr_color)

    if gsr_top:
        names = [r['name'][:40] + '...' if len(r['name']) > 40 else r['name'] for r in gsr_top]

        for i, r in enumerate(gsr_top):
            schnorr_eqs_all = r.get('schnorr_equivalents_all', [r['schnorr_equivalents']])
            y_pos = [i] * len(schnorr_eqs_all)

            ax2.scatter(schnorr_eqs_all, y_pos, color=gsr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5)
            ax2.scatter(r['schnorr_equivalents'], i, color=gsr_color, s=100, marker='D', edgecolors='black', linewidth=1, zorder=5)

        ax2.axvline(x=SCHNORR_BASELINE, color=schnorr_color, linestyle='--', linewidth=2, label=f'Block limit ({SCHNORR_BASELINE:,} sigs)')
        # Add legend entries for markers
        ax2.scatter([], [], color=gsr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5, label='Individual machines')
        ax2.scatter([], [], color=gsr_color, s=100, marker='D', edgecolors='black', linewidth=1, label='Mean across machines')
        ax2.set_yticks(range(len(names)))
        ax2.set_yticklabels(names, fontsize=8)
        ax2.set_xlabel('Schnorr Signature Equivalents (per block)')
        ax2.set_title('New Operations Added by GSR', fontsize=12, fontweight='bold', color=gsr_color)
        ax2.invert_yaxis()
        ax2.legend(loc='lower right', fontsize=8)
        ax2.grid(axis='x', alpha=0.3)
        ax2.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format(int(x), ',')))

    else:
        ax2.text(0.5, 0.5, 'No new GSR operations', ha='center', va='center', transform=ax2.transAxes)
        ax2.set_title('New Operations Added by GSR', fontsize=12, fontweight='bold', color=gsr_color)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")


def get_machine_sort_key(machine_info: dict) -> tuple:
    """
    Returns a sort key tuple: (arch_order, vendor_order, cpu_name)
    Groups: ARM first, then x86
    Subgroups: Apple, AMD, Intel, Other
    """
    cpu = machine_info['cpu'].lower()
    arch = machine_info['arch'].lower()
    
    if 'arm' in arch or 'aarch' in arch:
        arch_order = 0
    else:
        arch_order = 1
    
    if 'apple' in cpu or 'm1' in cpu or 'm2' in cpu or 'm3' in cpu or 'm4' in cpu:
        vendor_order = 0
    elif 'amd' in cpu or 'ryzen' in cpu:
        vendor_order = 1
    elif 'intel' in cpu or 'core' in cpu:
        vendor_order = 2
    else:
        vendor_order = 3
    
    return (arch_order, vendor_order, machine_info['cpu'])


def create_per_machine_visualization(all_machine_data: list[tuple], output_path: str):
    num_machines = len(all_machine_data)
    if num_machines == 0:
        return
    
    all_machine_data = sorted(all_machine_data, key=lambda x: get_machine_sort_key(x[1]))
    
    machine_names = []
    curr_worst_times = []
    curr_worst_names = []
    gsr_worst_times = []
    gsr_worst_names = []
    schnorr_times = []
    gsr_all_ops = []
    
    for results, machine_info in all_machine_data:
        cpu_short = machine_info['cpu'][:30] + '...' if len(machine_info['cpu']) > 30 else machine_info['cpu']
        machine_name = f"{cpu_short}\n({machine_info['arch']})"
        machine_names.append(machine_name)
        
        schnorr = get_schnorr_baseline(results)
        schnorr_times.append(schnorr['seconds'] if schnorr else 0)
        
        current_script, gsr_added = analyze_results(results)
        gsr_all_ops.append(gsr_added)
        
        if current_script:
            curr_worst_times.append(current_script[0]['seconds'])
            curr_worst_names.append(current_script[0]['name'][:25] + '...' if len(current_script[0]['name']) > 25 else current_script[0]['name'])
        else:
            curr_worst_times.append(0)
            curr_worst_names.append('N/A')
        
        if gsr_added:
            gsr_worst_times.append(gsr_added[0]['seconds'])
            gsr_worst_names.append(gsr_added[0]['name'][:25] + '...' if len(gsr_added[0]['name']) > 25 else gsr_added[0]['name'])
        else:
            gsr_worst_times.append(0)
            gsr_worst_names.append('N/A')
    
    fig, ax = plt.subplots(figsize=(14, max(6, num_machines * 1.5)))
    fig.suptitle('Worst Case Block Sized Script: Performance Across All Machines', 
                 fontsize=14, fontweight='bold')
    
    y_pos = range(num_machines)
    bar_height = 0.25
    
    curr_color = '#3498db'
    gsr_color = '#27ae60'
    schnorr_color = '#e74c3c'
    
    bars1 = ax.barh([y - bar_height for y in y_pos], curr_worst_times, bar_height, 
                    label='Current Script Worst (excluding sigops)', color=curr_color, alpha=0.8)
    bars2 = ax.barh([y for y in y_pos], gsr_worst_times, bar_height,
                    label='New GSR Worst', color=gsr_color, alpha=0.8)
    bars3 = ax.barh([y + bar_height for y in y_pos], schnorr_times, bar_height,
                    label='Schnorr Baseline', color=schnorr_color, alpha=0.8)
    
    for i, (bar1, bar2, bar3) in enumerate(zip(bars1, bars2, bars3)):
        if curr_worst_times[i] > 0:
            ax.text(bar1.get_width() + 0.02, bar1.get_y() + bar1.get_height()/2,
                   f'{curr_worst_times[i]:.2f}s', va='center', fontsize=8)
        if gsr_worst_times[i] > 0:
            gsr_label = f'{gsr_worst_times[i]:.2f}s'
            # Check if GSR worst case is the longest bar for this machine
            other_max = max(curr_worst_times[i], schnorr_times[i])
            if gsr_worst_times[i] > other_max and other_max > 0:
                factor = gsr_worst_times[i] / other_max
                # Find unique opcodes from GSR ops slower than other_max
                slower_opcodes = set(extract_opcode(op['name']) for op in gsr_all_ops[i] if op['seconds'] > other_max)
                slower_ops_str = ', '.join(sorted(slower_opcodes))
                gsr_label += f' ({factor:.1f}x slower: {slower_ops_str})'
            ax.text(bar2.get_width() + 0.02, bar2.get_y() + bar2.get_height()/2,
                   gsr_label, va='center', fontsize=8)
        if schnorr_times[i] > 0:
            ax.text(bar3.get_width() + 0.02, bar3.get_y() + bar3.get_height()/2,
                   f'{schnorr_times[i]:.2f}s', va='center', fontsize=8)
    
    ax.set_yticks(y_pos)
    ax.set_yticklabels(machine_names, fontsize=9)
    ax.set_xlabel('Execution Time (seconds)')
    ax.set_ylabel('Machine')
    ax.invert_yaxis()
    ax.grid(axis='x', alpha=0.3)
    
    legend_elements = [
        mpatches.Patch(facecolor=curr_color, alpha=0.8, label='Current Script Worst Case (excluding sigops)'),
        mpatches.Patch(facecolor=gsr_color, alpha=0.8, label='New GSR Worst Case'),
        mpatches.Patch(facecolor=schnorr_color, alpha=0.8, label='Schnorr Baseline')
    ]
    ax.legend(handles=legend_elements, loc='lower right')
    
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")
    
    print("\nPER-MACHINE WORST CASES")
    print(f"{'Machine':<40} {'Current Script':<15} {'New GSR':<15} {'Schnorr':<12}")
    for i, (results, machine_info) in enumerate(all_machine_data):
        cpu_short = machine_info['cpu'][:35] + '...' if len(machine_info['cpu']) > 35 else machine_info['cpu']
        print(f"{cpu_short:<40} {curr_worst_times[i]:<15.3f} {gsr_worst_times[i]:<15.3f} {schnorr_times[i]:<12.3f}")
    print()


def get_vendor_name(cpu: str) -> str:
    """Determine vendor from CPU name."""
    cpu_lower = cpu.lower()
    if 'apple' in cpu_lower or any(f'm{i}' in cpu_lower for i in range(1, 10)):
        return 'Apple'
    elif 'amd' in cpu_lower or 'ryzen' in cpu_lower:
        return 'AMD'
    elif 'intel' in cpu_lower or 'core' in cpu_lower:
        return 'Intel'
    elif 'rpi' in cpu_lower or 'raspberry' in cpu_lower or 'bcm' in cpu_lower or 'arm' in cpu_lower:
        return 'ARM (RPi/Other)'
    else:
        return 'Other'


def create_machine_scatter_plot(all_machine_data: list[tuple], output_path: str):
    """Create a scatter plot comparing current vs GSR worst case performance per machine."""
    if not all_machine_data:
        return
    
    # Collect data per machine
    machine_data = []
    for results, machine_info in all_machine_data:
        schnorr = get_schnorr_baseline(results)
        current_script, gsr_added = analyze_results(results)
        
        curr_worst_no_schnorr = current_script[0]['seconds'] if current_script else 0
        schnorr_time = schnorr['seconds'] if schnorr else 0
        # Current script worst case includes Schnorr
        curr_worst = max(curr_worst_no_schnorr, schnorr_time)
        gsr_worst = gsr_added[0]['seconds'] if gsr_added else 0
        
        vendor = get_vendor_name(machine_info['cpu'])
        cpu_short = machine_info['cpu'][:25] + '...' if len(machine_info['cpu']) > 25 else machine_info['cpu']
        
        machine_data.append({
            'cpu': cpu_short,
            'vendor': vendor,
            'arch': machine_info['arch'],
            'curr_worst': curr_worst,
            'gsr_worst': gsr_worst,
            'schnorr': schnorr_time,
            'gsr_name': gsr_added[0]['name'] if gsr_added else '',
            'curr_name': current_script[0]['name'] if current_script else '',
        })
    
    fig, ax = plt.subplots(figsize=(12, 10))
    fig.suptitle('Worst Case Block Sized Script: Machine Performance Comparison',
                 fontsize=14, fontweight='bold')
    
    # Vendor colors
    vendor_colors = {
        'Apple': '#666666',
        'AMD': '#ED1C24',
        'Intel': '#0071C5',
        'ARM (RPi/Other)': '#76B900',
        'Other': '#9B59B6'
    }
    
    # Scatter points per vendor
    for vendor in vendor_colors:
        vendor_machines = [m for m in machine_data if m['vendor'] == vendor]
        if not vendor_machines:
            continue
        
        x = [m['curr_worst'] for m in vendor_machines]
        y = [m['gsr_worst'] for m in vendor_machines]
        
        ax.scatter(x, y, c=vendor_colors[vendor], label=vendor, s=150, alpha=0.8, 
                   edgecolors='black', linewidth=1, zorder=5)
    
    # Add machine labels
    for m in machine_data:
        ax.annotate(m['cpu'], (m['curr_worst'], m['gsr_worst']),
                   xytext=(5, 5), textcoords='offset points', fontsize=7, alpha=0.8)
    
    # Diagonal parity line (GSR = Current)
    max_val = max(max(m['curr_worst'] for m in machine_data), 
                  max(m['gsr_worst'] for m in machine_data)) * 1.1
    ax.plot([0, max_val], [0, max_val], 'k--', alpha=0.5, linewidth=1.5, 
            label='Parity (GSR = Current)')
    
    # Add regions annotation
    ax.fill_between([0, max_val], [0, max_val], [max_val, max_val], 
                    alpha=0.1, color='red', label='GSR slower')
    ax.fill_between([0, max_val], [0, 0], [0, max_val], 
                    alpha=0.1, color='green', label='GSR faster')
    
    ax.set_xlabel('Current Script Worst Case incl. Schnorr (seconds)', fontsize=11)
    ax.set_ylabel('New GSR Worst Case (seconds)', fontsize=11)
    ax.set_xlim(0, max_val)
    ax.set_ylim(0, max_val)
    ax.set_aspect('equal')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper left', fontsize=9)
    
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")


def create_vendor_grouped_visualization(all_machine_data: list[tuple], output_path: str):
    """Create a visualization grouping machines by vendor (Apple, AMD, Intel)."""
    if not all_machine_data:
        return
    
    # Group machines by vendor
    vendor_data = defaultdict(lambda: {
        'curr_worst_times': [], 'gsr_worst_times': [], 'schnorr_times': [],
        'gsr_all_ops': [], 'machines': []
    })
    
    for results, machine_info in all_machine_data:
        vendor = get_vendor_name(machine_info['cpu'])
        
        schnorr = get_schnorr_baseline(results)
        current_script, gsr_added = analyze_results(results)
        
        vendor_data[vendor]['schnorr_times'].append(schnorr['seconds'] if schnorr else 0)
        vendor_data[vendor]['machines'].append(machine_info['cpu'])
        vendor_data[vendor]['gsr_all_ops'].extend(gsr_added)
        
        if current_script:
            vendor_data[vendor]['curr_worst_times'].append(current_script[0]['seconds'])
        if gsr_added:
            vendor_data[vendor]['gsr_worst_times'].append(gsr_added[0]['seconds'])
    
    # Calculate averages per vendor
    vendor_names = []
    avg_curr_worst = []
    avg_gsr_worst = []
    avg_schnorr = []
    vendor_gsr_ops = []
    machine_counts = []
    
    # Order: Apple, AMD, Intel, ARM/Other
    vendor_order = ['Apple', 'AMD', 'Intel', 'ARM (RPi/Other)', 'Other']
    for vendor in vendor_order:
        if vendor not in vendor_data:
            continue
        data = vendor_data[vendor]
        n = len(data['machines'])
        vendor_names.append(f"{vendor}\n({n} machine{'s' if n > 1 else ''})")
        machine_counts.append(n)
        
        avg_curr_worst.append(sum(data['curr_worst_times']) / len(data['curr_worst_times']) if data['curr_worst_times'] else 0)
        avg_gsr_worst.append(sum(data['gsr_worst_times']) / len(data['gsr_worst_times']) if data['gsr_worst_times'] else 0)
        avg_schnorr.append(sum(data['schnorr_times']) / len(data['schnorr_times']) if data['schnorr_times'] else 0)
        vendor_gsr_ops.append(data['gsr_all_ops'])
    
    num_vendors = len(vendor_names)
    fig, ax = plt.subplots(figsize=(12, max(5, num_vendors * 1.8)))
    fig.suptitle('Worst Case Block Sized Script: Performance by Hardware Vendor (Averaged)', 
                 fontsize=14, fontweight='bold')
    
    y_pos = range(num_vendors)
    bar_height = 0.25
    
    curr_color = '#3498db'
    gsr_color = '#27ae60'
    schnorr_color = '#e74c3c'
    
    bars1 = ax.barh([y - bar_height for y in y_pos], avg_curr_worst, bar_height, 
                    label='Current Script Worst (avg)', color=curr_color, alpha=0.8)
    bars2 = ax.barh([y for y in y_pos], avg_gsr_worst, bar_height,
                    label='New GSR Worst (avg)', color=gsr_color, alpha=0.8)
    bars3 = ax.barh([y + bar_height for y in y_pos], avg_schnorr, bar_height,
                    label='Schnorr Baseline (avg)', color=schnorr_color, alpha=0.8)
    
    for i, (bar1, bar2, bar3) in enumerate(zip(bars1, bars2, bars3)):
        if avg_curr_worst[i] > 0:
            ax.text(bar1.get_width() + 0.05, bar1.get_y() + bar1.get_height()/2,
                   f'{avg_curr_worst[i]:.2f}s', va='center', fontsize=9)
        if avg_gsr_worst[i] > 0:
            gsr_label = f'{avg_gsr_worst[i]:.2f}s'
            other_max = max(avg_curr_worst[i], avg_schnorr[i])
            if avg_gsr_worst[i] > other_max and other_max > 0:
                factor = avg_gsr_worst[i] / other_max
                # Find unique opcodes from GSR ops slower than other_max
                slower_opcodes = set(extract_opcode(op['name']) for op in vendor_gsr_ops[i] if op['seconds'] > other_max)
                if slower_opcodes:
                    slower_ops_str = ', '.join(sorted(slower_opcodes))
                    gsr_label += f' ({factor:.1f}x slower: {slower_ops_str})'
            ax.text(bar2.get_width() + 0.05, bar2.get_y() + bar2.get_height()/2,
                   gsr_label, va='center', fontsize=9)
        if avg_schnorr[i] > 0:
            ax.text(bar3.get_width() + 0.05, bar3.get_y() + bar3.get_height()/2,
                   f'{avg_schnorr[i]:.2f}s', va='center', fontsize=9)
    
    ax.set_yticks(y_pos)
    ax.set_yticklabels(vendor_names, fontsize=10)
    ax.set_xlabel('Execution Time (seconds)')
    ax.set_ylabel('Hardware Vendor')
    ax.invert_yaxis()
    ax.grid(axis='x', alpha=0.3)
    
    legend_elements = [
        mpatches.Patch(facecolor=curr_color, alpha=0.8, label='Current Script Worst Case (avg)'),
        mpatches.Patch(facecolor=gsr_color, alpha=0.8, label='New GSR Worst Case (avg)'),
        mpatches.Patch(facecolor=schnorr_color, alpha=0.8, label='Schnorr Baseline (avg)')
    ]
    ax.legend(handles=legend_elements, loc='lower right')
    
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")
    
    print("\nPER-VENDOR AVERAGED WORST CASES")
    print(f"{'Vendor':<20} {'Machines':<10} {'Current Script':<15} {'New GSR':<15} {'Schnorr':<12}")
    for i, vendor in enumerate(vendor_names):
        vendor_clean = vendor.split('\n')[0]
        print(f"{vendor_clean:<20} {machine_counts[i]:<10} {avg_curr_worst[i]:<15.3f} {avg_gsr_worst[i]:<15.3f} {avg_schnorr[i]:<12.3f}")
    print()


def main():
    if len(sys.argv) < 2:
        csv_paths = list(Path('.').glob('*.csv'))
        if not csv_paths:
            print("No CSV files found in current directory.")
            print("Usage: python3 visualize_bench.py <csv_file1> [csv_file2] [csv_file3] ...")
            print("       Or provide a directory: python3 visualize_bench.py <directory>")
            print("       Or run with no arguments to automatically use all CSV files in current directory.")
            sys.exit(1)
        csv_paths = [str(path) for path in csv_paths]
    else:
        first_arg = Path(sys.argv[1])
        if len(sys.argv) == 2 and first_arg.is_dir():
            csv_paths = list(first_arg.glob('*.csv'))
            if not csv_paths:
                print(f"No CSV files found in directory: {first_arg}")
                print("Usage: python3 visualize_bench.py <csv_file1> [csv_file2] [csv_file3] ...")
                print("       Or provide a directory: python3 visualize_bench.py <directory>")
                print("       Or run with no arguments to automatically use all CSV files in current directory.")
                sys.exit(1)
            csv_paths = [str(path) for path in csv_paths]
        else:
            csv_paths = sys.argv[1:]
    
    for path in csv_paths:
        if not Path(path).exists():
            print(f"Error: CSV file not found: {path}")
            sys.exit(1)
    
    print(f"Reading benchmark results from {len(csv_paths)} file(s):")
    for path in csv_paths:
        print(f"  - {path}")
    
    averaged_results, all_machine_data = parse_multiple_csvs(csv_paths)
    
    schnorr = get_schnorr_baseline(averaged_results)
    current_script, gsr_added = analyze_results(averaged_results)
    
    print_summary(current_script, gsr_added, schnorr, len(csv_paths))
    
    plots_dir = Path('plots')
    plots_dir.mkdir(exist_ok=True)
    
    output_base = 'benchmark_analysis' if len(csv_paths) > 1 else Path(csv_paths[0]).stem + '_analysis'
    create_averaged_visualization(current_script, gsr_added, schnorr, len(csv_paths), f'plots/{output_base}_seconds.png')
    create_schnorr_equivalents_visualization(current_script, gsr_added, schnorr, len(csv_paths), f'plots/{output_base}_schnorr_units.png')

    if len(csv_paths) > 1:
        create_per_machine_visualization(all_machine_data, f'plots/{output_base}_per_machine.png')
        create_vendor_grouped_visualization(all_machine_data, f'plots/{output_base}_by_vendor.png')
        create_machine_scatter_plot(all_machine_data, f'plots/{output_base}_scatter.png')
    else:
        create_per_machine_visualization(all_machine_data, f'plots/{output_base}_machine.png')
        create_machine_scatter_plot(all_machine_data, f'plots/{output_base}_scatter.png')


if __name__ == '__main__':
    main()
