# Description: This script is used to simulate the full model of the robot in mujoco

# Authors:
# - Giulio Turrisi
# - Daniel Ordoñez Apraez
import os
import time

import mujoco
import numpy as np

import config as cfg

# Parameters for both MPC and simulation
from helpers.foothold_reference_generator import FootholdReferenceGenerator
from helpers.periodic_gait_generator import PeriodicGaitGenerator
from helpers.srb_inertia_computation import SrbInertiaComputation
from helpers.swing_trajectory_controller import SwingTrajectoryController
from simulation.quadruped_env import QuadrupedEnv
from utils.math_utils import skew
from utils.mujoco_visual import plot_swing_mujoco
from utils.quadruped_utils import GaitType, LegsAttr, estimate_terrain_slope

# TODO: Why is this here?
os.environ['XLA_FLAGS'] = ('--xla_gpu_triton_gemm_any=True')

np.set_printoptions(precision=3, suppress=True)


# Main simulation loop ------------------------------------------------------------------
def update_contact_sequence(pgg, horizon, mpc_dt, simulation_dt):
    if gait_name == "full_stance":
        contact_sequence = np.ones((4, horizon * 2))
    else:
        contact_sequence = pgg.compute_contact_sequence(mpc_dt=mpc_dt, simulation_dt=simulation_dt)

    # in the case of nonuniform discretization, we need to subsample the contact sequence
    if (cfg.mpc_params['use_nonuniform_discretization']):
        subsample_step_contact_sequence = int(cfg.mpc_params['dt_fine_grained'] / mpc_dt)
        if (subsample_step_contact_sequence > 1):
            contact_sequence_fine_grained = contact_sequence[:, ::subsample_step_contact_sequence][:,
                                            0:cfg.mpc_params['horizon_fine_grained']]
        else:
            contact_sequence_fine_grained = contact_sequence[:, 0:cfg.mpc_params['horizon_fine_grained']]

        subsample_step_contact_sequence = int(cfg.mpc_params['dt'] / mpc_dt)
        if (subsample_step_contact_sequence > 1):
            contact_sequence = contact_sequence[:, ::subsample_step_contact_sequence]
        contact_sequence = contact_sequence[:, 0:horizon]
        contact_sequence[:, 0:cfg.mpc_params['horizon_fine_grained']] = contact_sequence_fine_grained
    return contact_sequence


def get_gait_params(gait_type: str) -> [GaitType, float, float]:
    if gait_type == "trot":
        step_frequency = 1.3
        duty_factor = 0.65
        gait_type = GaitType.TROT
    elif gait_type == "crawl":
        step_frequency = 0.7
        duty_factor = 0.9
        gait_type = GaitType.BACKDIAGONALCRAWL
    elif gait_type == "pace":
        step_frequency = 2
        duty_factor = 0.7
        gait_type = GaitType.PACE
    elif gait_type == "bound":
        step_frequency = 2
        duty_factor = 0.65
        gait_type = GaitType.BOUNDING
    else:
        step_frequency = 2
        duty_factor = 0.65
        gait_type = GaitType.FULL_STANCE
        # print("FULL STANCE")
    return gait_type, duty_factor, step_frequency


if __name__ == '__main__':

    robot_name = cfg.robot
    robot_legs_joints = cfg.robot_leg_joints
    scene_name = cfg.simulation_params['scene']
    simulation_dt = cfg.simulation_params['dt']

    # Create the quadruped robot environment. _______________________________________________________________________
    env = QuadrupedEnv(robot=robot_name,
                       # TODO: This should come from a cfg.robot. Not hardcoded.
                       legs_joint_names=LegsAttr(**robot_legs_joints),
                       scene=scene_name,
                       base_vel_range=(-0.3, 0.8),
                       sim_dt=simulation_dt,
                       base_vel_command_type="forward",
                       feet_geom_name=LegsAttr(FL='FL', FR='FR', RL='RL', RR='RR'),  # Geom/Frame id of feet
                       )
    env.reset()
    env.render()
    mass = np.sum(env.mjModel.body_mass)

    # _______________________________________________________________________________________________________________

    # TODO: CONTROLLER INITIALIZATION CODE THAT SHOULD BE REMOVED FROM HERE.
    #  Here we should simply initialize the selected controller with the desired configuration E.g.:
    #   if cfg.controller_name == "A"
    #       controller = ControlerA(**cfg.controller)
    #   elif cfg.controller_name == "B"
    #       controller = ControlerB(**cfg.controller)
    #   ... __________________________________________________________________________________________________
    mpc_frequency = cfg.simulation_params['mpc_frequency']
    # MPC Magic - i took the minimum value of the dt
    # used along the horizon of the MPC
    if cfg.mpc_params['use_nonuniform_discretization']:
        mpc_dt = cfg.mpc_params['dt_fine_grained']
    else:
        mpc_dt = cfg.mpc_params['dt']
    horizon = cfg.mpc_params['horizon']

    # input_rates optimize the delta_GRF (smoooth!)
    # nominal optimize directly the GRF (not smooth)
    # sampling use GPUUUU
    # collaborative optimize directly the GRF and has a passive arm model inside
    if cfg.mpc_params['type'] == 'nominal':
        from gradient.nominal.centroidal_nmpc_nominal import Acados_NMPC_Nominal

        controller = Acados_NMPC_Nominal()

        if cfg.mpc_params['optimize_step_freq']:
            from gradient.nominal.centroidal_nmpc_gait_adaptive import Acados_NMPC_GaitAdaptive

            batched_controller = Acados_NMPC_GaitAdaptive()

    elif cfg.mpc_params['type'] == 'input_rates':
        from gradient.input_rates.centroidal_nmpc_input_rates import Acados_NMPC_InputRates

        controller = Acados_NMPC_InputRates()

        if cfg.mpc_params['optimize_step_freq']:
            from gradient.nominal.centroidal_nmpc_gait_adaptive import Acados_NMPC_GaitAdaptive

            batched_controller = Acados_NMPC_GaitAdaptive()

    elif cfg.mpc_params['type'] == 'sampling':
        if cfg.mpc_params['optimize_step_freq']:
            from sampling.centroidal_nmpc_jax_gait_adaptive import Sampling_MPC
        else:
            from sampling.centroidal_nmpc_jax import Sampling_MPC

        import jax
        import jax.numpy as jnp

        num_parallel_computations = cfg.mpc_params['num_parallel_computations']
        iteration = cfg.mpc_params['num_sampling_iterations']
        controller = Sampling_MPC(horizon=horizon,
                                  dt=mpc_dt,
                                  num_parallel_computations=num_parallel_computations,
                                  sampling_method=cfg.mpc_params['sampling_method'],
                                  control_parametrization=cfg.mpc_params['control_parametrization'],
                                  device="gpu")
        best_control_parameters = jnp.zeros((controller.num_control_parameters,))
        jitted_compute_control = jax.jit(controller.compute_control, device=controller.device)
        # jitted_get_key = jax.jit(controller.get_key, device=controller.device)
        jitted_prepare_state_and_reference = controller.prepare_state_and_reference

        index_shift = 0


    # Periodic gait generator
    gait_name = cfg.simulation_params['gait']
    gait_type, duty_factor, step_frequency = get_gait_params(gait_name)
    # __________________________________________________________________________________________________

    # Periodic gait generator _________________________________________________________________________
    # Given the possibility to use nonuniform discretization, we generate a contact sequence two times longer
    pgg = PeriodicGaitGenerator(duty_factor=duty_factor, step_freq=step_frequency, gait_type=gait_type,
                                horizon=horizon * 2)
    contact_sequence = pgg.compute_contact_sequence(mpc_dt=mpc_dt, simulation_dt=simulation_dt)
    nominal_sample_freq = step_frequency
    # Create the foothold reference generator
    stance_time = (1 / step_frequency) * duty_factor
    frg = FootholdReferenceGenerator(stance_time=stance_time)

    # Create swing trajectory generator
    step_height = cfg.simulation_params['step_height']
    swing_period = (1 - duty_factor) * (1 / step_frequency)  # + 0.07
    position_gain_fb = cfg.simulation_params['swing_position_gain_fb']
    velocity_gain_fb = cfg.simulation_params['swing_velocity_gain_fb']
    swing_generator = cfg.simulation_params['swing_generator']
    stc = SwingTrajectoryController(step_height=step_height, swing_period=swing_period,
                                    position_gain_fb=position_gain_fb, velocity_gain_fb=velocity_gain_fb,
                                    generator=swing_generator)
    # Swing controller variables
    swing_time = [0, 0, 0, 0]
    lift_off_positions = env.feet_pos(frame='world')

    # Terrain estimator
    z_foot_mean = 0.0

    # Online computation of the inertia parameter
    srb_inertia_computation = SrbInertiaComputation()  # TODO: This seems to be unsused.
    inertia = cfg.inertia

    # Initialization of variables used in the main control loop
    # ____________________________________________________________
    # Set the reference for the state
    ref_pose = np.array([0, 0, cfg.simulation_params['ref_z']])
    ref_linear_velocity = env.target_base_vel
    ref_orientation = np.array([cfg.simulation_params['ref_roll'], cfg.simulation_params['ref_pitch'], 0])
    ref_angular_velocity = np.array([0, 0, cfg.simulation_params['ref_yaw_dot']])
    # # SET REFERENCE AS DICTIONARY
    # TODO: I would suggest to create a DataClass for "BaseConfig" used in the PotatoModel controllers.
    ref_state = {'ref_position':         ref_pose,
                 'ref_linear_velocity':  ref_linear_velocity,
                 'ref_orientation':      ref_orientation,
                 'ref_angular_velocity': ref_angular_velocity,
                 }

    # Starting contact sequence
    previous_contact = np.array([1, 1, 1, 1])
    previous_contact_mpc = np.array([1, 1, 1, 1])
    current_contact = np.array([1, 1, 1, 1])

    nmpc_GRFs = np.zeros((12,))
    nmpc_wrenches = np.zeros((6,))
    nmpc_footholds = np.zeros((12,))

    # Jacobian matrices
    jac_feet_prev = LegsAttr(*[np.zeros((3, env.mjModel.nv)) for _ in range(4)])
    jac_feet_dot = LegsAttr(*[np.zeros((3, env.mjModel.nv)) for _ in range(4)])
    # Torque vector
    tau = LegsAttr(*[np.zeros((env.mjModel.nv, 1)) for _ in range(4)])
    # State
    state_current, state_prev = {}, {}
    feet_pos = None
    feet_traj_geom_ids = None
    legs_order = ["FL", "FR", "RL", "RR"]

    RENDER_FREQ = 30  # Hz
    last_render_time = time.time()

    while True:
        step_start = time.time()

        # Update the robot state --------------------------------
        feet_pos = env.feet_pos(frame='world')
        hip_pos = env.hip_positions(frame='world')

        state_current = dict(
            position=env.base_pos,
            linear_velocity=env.base_lin_vel,
            orientation=env.base_ori_euler_xyz,
            angular_velocity=env.base_ang_vel,
            foot_FL=feet_pos.FL,
            foot_FR=feet_pos.FR,
            foot_RL=feet_pos.RL,
            foot_RR=feet_pos.RR
            )
        # -------------------------------------------------------

        # Update the desired contact sequence ---------------------------
        # Update the periodic gait generator
        pgg.run(simulation_dt, pgg.step_freq)
        contact_sequence = update_contact_sequence(pgg, horizon, mpc_dt, simulation_dt)

        previous_contact = current_contact
        current_contact = np.array([contact_sequence[0][0],
                                    contact_sequence[1][0],
                                    contact_sequence[2][0],
                                    contact_sequence[3][0]])

        # Compute the reference for the footholds ---------------------------------------------------
        ref_feet_pos = frg.compute_footholds_reference(
            com_position=env.base_pos,
            rpy_angles=env.base_ori_euler_xyz,
            linear_com_velocity=env.base_lin_vel[0:2],
            desired_linear_com_velocity=env.target_base_vel[0:2],
            hips_position=hip_pos,
            com_height=state_current["position"][2],
            lift_off_positions=lift_off_positions)

        # Update state reference
        ref_state |= dict(ref_foot_FL=ref_feet_pos.FL.reshape((1, 3)),
                          ref_foot_FR=ref_feet_pos.FR.reshape((1, 3)),
                          ref_foot_RL=ref_feet_pos.RL.reshape((1, 3)),
                          ref_foot_RR=ref_feet_pos.RR.reshape((1, 3)),
                          # Also update the reference base linear velocity and # TODO: orientation.
                          ref_linear_velocity=env.target_base_vel)
        # -------------------------------------------------------------------------------------------------
        # Estimate the terrain slope and elevation -------------------------------------------------------
        roll, pitch = estimate_terrain_slope(
            base_position=env.base_pos,
            yaw=env.base_ori_euler_xyz[2],
            feet_pos=lift_off_positions)
        ref_state["ref_orientation"] = np.array([roll, pitch, 0])

        # Update the reference height given the foot in contact
        num_feet_in_contact = np.sum(current_contact)
        feet_pos_z = np.asarray(feet_pos.to_list(order=legs_order))[:, 2]
        if num_feet_in_contact != 0:
            # TODO: Is this a moving average ?
            z_foot_mean_temp = np.sum(feet_pos_z * current_contact) / num_feet_in_contact
            z_foot_mean = z_foot_mean_temp * 0.4 + z_foot_mean * 0.6
        ref_state["ref_position"][2] = cfg.simulation_params['ref_z'] + z_foot_mean
        # -------------------------------------------------------------------------------------------------

        # TODO: WTF is this ? Need documentation
        if cfg.mpc_params['type'] == 'sampling':
            if cfg.mpc_params['shift_solution']:
                index_shift += 0.05
                best_control_parameters = controller.shift_solution(best_control_parameters, index_shift)

        # TODO: this should be hidden inside the controller forward/get_action method
        # Solve OCP ---------------------------------------------------------------------------------------
        if env.step_num % round(1 / (mpc_frequency * simulation_dt)) == 0:

            # We can recompute the inertia of the single rigid body model
            # or use the fixed one in cfg.py
            if (cfg.simulation_params['use_inertia_recomputation']):
                # TODO: d.qpos is not defined
                inertia = srb_inertia_computation.compute_inertia(d.qpos)

            if ((cfg.mpc_params['optimize_step_freq'])):
                # we can always optimize the step freq, or just at the apex of the swing
                # to avoid possible jittering in the solution
                optimize_swing = 0  # 1 for always, 0 for apex
                for leg_id in range(4):
                    # Swing time check
                    if (current_contact[leg_id] == 0):
                        if ((swing_time[leg_id] > (swing_period / 2.) - 0.02) and \
                                (swing_time[leg_id] < (swing_period / 2.) + 0.02)):
                            optimize_swing = 1
                            nominal_sample_freq = step_frequency

            # If we use sampling
            if (cfg.mpc_params['type'] == 'sampling'):

                time_start = time.time()
                # Convert data to jax
                state_current_jax, \
                    reference_state_jax, \
                    best_control_parameters = jitted_prepare_state_and_reference(state_current, ref_state,
                                                                                 best_control_parameters,
                                                                                 current_contact, previous_contact_mpc)

                for iter_sampling in range(iteration):
                    if (cfg.mpc_params['sampling_method'] == 'cem_mppi'):
                        if (iter_sampling == 0):
                            controller = controller.with_newsigma(cfg.mpc_params['sigma_cem_mppi'])
                        nmpc_GRFs, \
                            nmpc_footholds, \
                            best_control_parameters, \
                            best_cost, \
                            best_sample_freq, \
                            costs, \
                            sigma_cem_mppi = jitted_compute_control(state_current_jax, reference_state_jax,
                                                                    contact_sequence, best_control_parameters,
                                                                    controller.master_key, controller.sigma_cem_mppi)
                        controller = controller.with_newsigma(sigma_cem_mppi)
                    else:
                        nmpc_GRFs, \
                            nmpc_footholds, \
                            best_control_parameters, \
                            best_cost, \
                            best_sample_freq, \
                            costs = jitted_compute_control(state_current_jax, reference_state_jax, contact_sequence,
                                                           best_control_parameters, controller.master_key, pgg.get_t(),
                                                           nominal_sample_freq, optimize_swing)

                    controller = controller.with_newkey()

                if ((cfg.mpc_params['optimize_step_freq']) and (optimize_swing == 1)):
                    pgg.step_freq = np.array([best_sample_freq])[0]
                    nominal_sample_freq = pgg.step_freq
                    stance_time = (1 / pgg.step_freq) * duty_factor
                    frg.stance_time = stance_time

                    swing_period = (1 - duty_factor) * (1 / pgg.step_freq)  # + 0.07
                    stc.regenerate_swing_trajectory_generator(step_height=step_height, swing_period=swing_period)

                nmpc_footholds = ref_feet_pos

                nmpc_GRFs = np.array(nmpc_GRFs)

                previous_contact_mpc = current_contact
                index_shift = 0
                # optimizer_cost = best_cost

            # If we use Gradient-Based MPC
            else:
                time_start = time.time()
                nmpc_GRFs, nmpc_footholds, _, status = controller.compute_control(
                    state_current,
                    ref_state,
                    contact_sequence,
                    inertia=inertia.flatten()
                    )
                # TODO functions should output this class instance.
                nmpc_footholds = LegsAttr(FL=nmpc_footholds[0],
                                          FR=nmpc_footholds[1],
                                          RL=nmpc_footholds[2],
                                          RR=nmpc_footholds[3])

                # optimizer_cost = controller.acados_ocp_solver.get_cost()

                if cfg.mpc_params['optimize_step_freq'] and optimize_swing == 1:
                    contact_sequence_temp = np.zeros((len(cfg.mpc_params['step_freq_available']), 4, horizon * 2))
                    for j in range(len(cfg.mpc_params['step_freq_available'])):
                        pgg_temp = PeriodicGaitGenerator(duty_factor=duty_factor,
                                                         step_freq=cfg.mpc_params['step_freq_available'][j],
                                                         gait_type=gait_type,
                                                         horizon=horizon * 2)
                        pgg_temp.phase_signal = pgg.phase_signal
                        pgg_temp.init = pgg.init
                        contact_sequence_temp[j] = pgg_temp.compute_contact_sequence(mpc_dt=mpc_dt,
                                                                                     simulation_dt=simulation_dt)

                    costs, best_sample_freq = batched_controller.compute_batch_control(state_current, ref_state,
                                                                                       contact_sequence_temp)

                    pgg.step_freq = best_sample_freq
                    stance_time = (1 / pgg.step_freq) * duty_factor
                    frg.stance_time = stance_time
                    swing_period = (1 - duty_factor) * (1 / pgg.step_freq)  # + 0.07
                    stc.regenerate_swing_trajectory_generator(step_height=step_height, swing_period=swing_period)

                # If the controller is using RTI, we need to linearize the mpc after its computation
                # this helps to minize the delay between new state->control, but only in a real case.
                # Here we are in simulation and does not make any difference for now
                if (controller.use_RTI):
                    # preparation phase
                    controller.acados_ocp_solver.options_set('rti_phase', 1)
                    status = controller.acados_ocp_solver.solve()
                    # print("preparation phase time: ", controller.acados_ocp_solver.get_stats('time_tot'))

            # TODO: Indexing should not be hardcoded. Env should provide indexing of leg actuator dimensions.
            nmpc_GRFs = LegsAttr(FL=nmpc_GRFs[0:3] * current_contact[0],
                                 FR=nmpc_GRFs[3:6] * current_contact[1],
                                 RL=nmpc_GRFs[6:9] * current_contact[2],
                                 RR=nmpc_GRFs[9:12] * current_contact[3])

            # Compute the linear and angular components of the wrench. This goes to the estimator!
            wrench_lin = np.sum(nmpc_GRFs.to_list(), axis=0)
            feet_pos_base = env.feet_pos(frame='base')
            wrench_ang = np.sum([skew(feet_pos_base[leg_name]) @ nmpc_GRFs[leg_name] for leg_name in legs_order],
                                axis=0)
            nmpc_wrenches = np.concatenate((wrench_lin, wrench_ang.flatten()), axis=0)
        # -------------------------------------------------------------------------------------------------

        # Compute Stance Torque ---------------------------------------------------------------------------
        feet_jac = env.feet_jacobians(frame='world', return_rot_jac=False)
        # Compute feet velocities
        feet_vel = LegsAttr(**{leg_name: feet_jac[leg_name] @ env.mjData.qvel for leg_name in legs_order})
        # Compute jacobian derivatives of the contact points
        jac_feet_dot = (feet_jac - jac_feet_prev) / simulation_dt  # Finite difference approximation
        jac_feet_prev = feet_jac  # Update previous Jacobians
        # Compute the torque with the contact jacobian (-J.T @ f)   J: R^nv -> R^3,   f: R^3
        tau.FL = -np.matmul(feet_jac.FL[:, env.legs_qvel_idx.FL].T, nmpc_GRFs.FL)
        tau.FR = -np.matmul(feet_jac.FR[:, env.legs_qvel_idx.FR].T, nmpc_GRFs.FR)
        tau.FR = -np.matmul(feet_jac.FR[:, env.legs_qvel_idx.FR].T, nmpc_GRFs.FR)
        tau.RL = -np.matmul(feet_jac.RL[:, env.legs_qvel_idx.RL].T, nmpc_GRFs.RL)
        tau.RR = -np.matmul(feet_jac.RR[:, env.legs_qvel_idx.RR].T, nmpc_GRFs.RR)
        # ---------------------------------------------------------------------------------------------------

        # Compute Swing Torque ------------------------------------------------------------------------------
        # TODO: Move contact sequence to labels FL, FR, RL, RR instead of a fixed indexing.
        for leg_id, leg_name in enumerate(legs_order):
            # Swing time reset
            if current_contact[leg_id] == 0:
                if swing_time[leg_id] < swing_period:
                    swing_time[leg_id] = swing_time[leg_id] + simulation_dt
            else:
                swing_time[leg_id] = 0
            # Set lif-offs
            if previous_contact[leg_id] == 1 and current_contact[leg_id] == 0:
                lift_off_positions[leg_name] = feet_pos[leg_name]

        # The swing controller is in the end-effector space. For its computation,
        # we save for simplicity joints position and velocities
        qpos, qvel = env.mjData.qpos, env.mjData.qvel
        # centrifugal, coriolis, gravity
        legs_qfrc_bias = LegsAttr(FL=env.mjData.qfrc_bias[env.legs_qvel_idx.FL],
                                  FR=env.mjData.qfrc_bias[env.legs_qvel_idx.FR],
                                  RL=env.mjData.qfrc_bias[env.legs_qvel_idx.RL],
                                  RR=env.mjData.qfrc_bias[env.legs_qvel_idx.RR])
        # and inertia matrix
        mass_matrix = np.zeros((env.mjModel.nv, env.mjModel.nv))
        mujoco.mj_fullM(env.mjModel, mass_matrix, env.mjData.qM)
        # Get the mass matrix of the legs
        legs_mass_matrix = LegsAttr(FL=mass_matrix[np.ix_(env.legs_qvel_idx.FL, env.legs_qvel_idx.FL)],
                                    FR=mass_matrix[np.ix_(env.legs_qvel_idx.FR, env.legs_qvel_idx.FR)],
                                    RL=mass_matrix[np.ix_(env.legs_qvel_idx.RL, env.legs_qvel_idx.RL)],
                                    RR=mass_matrix[np.ix_(env.legs_qvel_idx.RR, env.legs_qvel_idx.RR)])

        for leg_id, leg_name in enumerate(legs_order):
            if current_contact[leg_id] == 0:  # If in swing phase, compute the swing trajectory tracking control.
                tau[leg_name], _, _ = stc.compute_swing_control(
                    model=env.mjModel,
                    q=qpos[env.legs_qpos_idx[leg_name]],
                    q_dot=qvel[env.legs_qvel_idx[leg_name]],
                    J=feet_jac[leg_name][:, env.legs_qvel_idx[leg_name]],
                    J_dot=jac_feet_dot[leg_name][:, env.legs_qvel_idx[leg_name]],
                    lift_off=lift_off_positions[leg_name],
                    touch_down=nmpc_footholds[leg_name],
                    swing_time=swing_time[leg_id],
                    foot_pos=feet_pos[leg_name],
                    foot_vel=feet_vel[leg_name],
                    h=legs_qfrc_bias[leg_name],
                    mass_matrix=legs_mass_matrix[leg_name]
                    )
        # ---------------------------------------------------------------------------------------------------
        # Set control and mujoco step ----------------------------------------------------------------------
        # TODO: The order of the action space should not be hardoded, it should be provided by the environment.
        action = np.concatenate((tau.to_list(order=["FR", "FL", "RR", "RL"]))).reshape(env.mjModel.nu)

        obs, reward, is_terminated, is_truncated, info = env.step(action=action)

        # Render only at a certain frequency
        if time.time() - last_render_time > 1.0 / RENDER_FREQ or env.step_num == 1:
            feet_traj_geom_ids = plot_swing_mujoco(viewer=env.viewer,
                                                   swing_traj_controller=stc,
                                                   swing_period=swing_period,
                                                   swing_time=LegsAttr(FL=swing_time[0],
                                                                       FR=swing_time[1],
                                                                       RL=swing_time[2],
                                                                       RR=swing_time[3]),
                                                   lift_off_positions=lift_off_positions,
                                                   nmpc_footholds=nmpc_footholds,
                                                   ref_feet_pos=ref_feet_pos,
                                                   geom_ids=feet_traj_geom_ids)
            env.render()
            last_render_time = time.time()

        if env.step_num > 2000 or is_terminated or is_truncated:
            if is_terminated:
                print("Environment terminated")
            env.reset()
            pgg.reset()
            lift_off_positions = env.feet_pos(frame='world')
            current_contact = np.array([0, 0, 0, 0])
            previous_contact = np.asarray(current_contact)
            z_foot_mean = 0.0
        # print("loop time: ", time.time() - step_start)
